"""Phase 4 exit test: the LLM plans and explains; everything between is deterministic.

Runs offline with FakeLLM (canned, schema-valid plans; the template explainer writes claims
from receipts). A live twin with the real model is marked `live`.

Deviation from the spec (see DECISIONS, Phase 4): EIA generation lags a day, so at
2021-02-15T12:00Z the fuel collapse was not yet observable. H2 is asserted on top at
2021-02-16T12:00Z; at 02-15T12:00Z the test only requires H2 and H4 to survive.
"""

from __future__ import annotations

import os
from datetime import datetime

import duckdb
import pytest

from gridcase.explain.schema import Claim, Conclusion
from gridcase.explain.verifier import verify
from gridcase.investigate import investigate, recheck
from gridcase.memory.cases import close_case, effective_prior, get_case, new_case
from gridcase.planner.llm import FakeLLM, default_plan_response
from gridcase.planner.schema import InvestigationPlan, ToolStep
from gridcase.planner.validate import validate_plan

URI_EARLY = datetime(2021, 2, 15, 12)
URI_AS_OF = datetime(2021, 2, 16, 12)
URI_RECHECK = datetime(2021, 2, 18, 12)


@pytest.fixture()
def store(fixture_store: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    fixture_store.execute("DELETE FROM cases")
    fixture_store.execute("DELETE FROM ledger")
    fixture_store.execute("DELETE FROM scheduled_tasks")
    fixture_store.execute("DELETE FROM learned_priors")
    return fixture_store


def test_1_uri_replay_plans_validly_and_concludes_supply_shortfall(
    store: duckdb.DuckDBPyConnection,
) -> None:
    llm = FakeLLM()
    case = investigate(store, llm, entity="ERCO", metric="forecast_error_mw", as_of=URI_AS_OF)
    assert case.state == "action_taken"
    assert case.plan is not None and case.plan_attempts <= 2
    assert len(case.plan.steps) <= 12
    assert case.conclusion is not None
    ranked = [h.id for h in case.conclusion.ranked if h.status != "refuted"]
    assert ranked[0] == "H2_SUPPLY_SHORTFALL", case.conclusion.ranked
    assert "H4_LOAD_SHED" in ranked
    assert llm.calls <= 4


def test_1b_at_noon_on_the_15th_supply_and_load_shed_both_survive(
    store: duckdb.DuckDBPyConnection,
) -> None:
    case = investigate(store, FakeLLM(), entity="ERCO", metric="forecast_error_mw", as_of=URI_EARLY)
    assert case.conclusion is not None
    status = {h.id: h.status for h in case.conclusion.ranked}
    # EIA's day-lagged generation cannot yet show the shortfall: H2 must stay open, not refuted
    assert status["H2_SUPPLY_SHORTFALL"] != "refuted"
    assert status["H4_LOAD_SHED"] != "refuted"
    survivors = [h.id for h in case.conclusion.ranked if h.status != "refuted"]
    assert {"H2_SUPPLY_SHORTFALL", "H4_LOAD_SHED"} & set(survivors[:2])


def test_2_every_claim_cites_a_receipt_and_uncited_claims_are_dropped(
    store: duckdb.DuckDBPyConnection,
) -> None:
    case = investigate(store, FakeLLM(), entity="ERCO", metric="forecast_error_mw", as_of=URI_AS_OF)
    assert case.conclusion is not None and case.conclusion.claims
    assert all(c.receipt_ids for c in case.conclusion.claims if c.tier != "inferred")
    receipts = {r.receipt_id for r in case.receipts}
    assert all(set(c.receipt_ids) <= receipts for c in case.conclusion.claims)
    # inject an uncited claim and one citing a receipt that does not exist
    tampered = Conclusion(
        **case.conclusion.model_dump()
        | {
            "claims": [
                *case.conclusion.claims,
                Claim(
                    text="Demand fell 40,000 MW because of aliens.", receipt_ids=[], tier="governed"
                ),
                Claim(
                    text="Wind fell 90%.", receipt_ids=["deadbeefdeadbeefdeadbeef"], tier="derived"
                ),
            ]
        }
    )
    verified, dropped = verify(tampered, {r.receipt_id: r for r in case.receipts})
    texts = [c.text for c in verified.claims]
    assert "Wind fell 90%." not in texts
    aliens = next(c for c in verified.claims if c.text.startswith("Demand fell 40,000"))
    assert aliens.tier == "inferred"
    assert len(dropped) == 1


def test_3_validator_rejects_four_bad_plans_with_distinct_codes() -> None:
    good = default_plan_response(case_id="c", entity="ERCO", as_of=URI_AS_OF)
    assert validate_plan(good) == []

    no_h0 = good.model_copy(
        update={"hypotheses": [h for h in good.hypotheses if h != "H0_DATA_ARTIFACT"]}
    )
    one_hyp = good.model_copy(update={"hypotheses": ["H0_DATA_ARTIFACT"], "steps": good.steps[:2]})
    cyc_steps = [s.model_copy() for s in good.steps]
    cyc_steps[0] = cyc_steps[0].model_copy(update={"depends_on": [cyc_steps[1].step_id]})
    cyc_steps[1] = cyc_steps[1].model_copy(update={"depends_on": [cyc_steps[0].step_id]})
    cycle = good.model_copy(update={"steps": cyc_steps})
    bad_tool = good.model_copy(
        update={
            "steps": [
                *good.steps,
                ToolStep(
                    step_id="x", tool="magic.wand", inputs={}, hypothesis_ids=["H0_DATA_ARTIFACT"]
                ),
            ]
        }
    )
    codes = {
        name: {e.code for e in validate_plan(p)}
        for name, p in [
            ("no_h0", no_h0),
            ("one_hyp", one_hyp),
            ("cycle", cycle),
            ("bad_tool", bad_tool),
        ]
    }
    assert "E_NO_H0" in codes["no_h0"]
    assert "E_TOO_FEW_HYP" in codes["one_hyp"]
    assert "E_CYCLE" in codes["cycle"]
    assert "E_UNKNOWN_TOOL" in codes["bad_tool"]
    first = [next(iter(c)) for c in codes.values()]
    assert (
        len(
            {"E_NO_H0", "E_TOO_FEW_HYP", "E_CYCLE", "E_UNKNOWN_TOOL"} & set().union(*codes.values())
        )
        == 4
    )
    assert len(first) == 4


def test_4_recheck_confirms_supply_shortfall_refutes_artifact_and_closes(
    store: duckdb.DuckDBPyConnection,
) -> None:
    llm = FakeLLM()
    case = investigate(store, llm, entity="ERCO", metric="forecast_error_mw", as_of=URI_AS_OF)
    result = recheck(store, case.case_id, as_of=URI_RECHECK)
    assert result.verdicts["H2_SUPPLY_SHORTFALL"] == "confirmed"
    assert result.verdicts["H0_DATA_ARTIFACT"] == "refuted"
    assert get_case(store, case.case_id).state == "closed"
    assert llm.calls <= 4  # the recheck is deterministic: no new LLM calls


def test_5_closing_weather_cases_raises_the_weather_prior(store: duckdb.DuckDBPyConnection) -> None:
    before = effective_prior(store, "H1_WEATHER_DEMAND", "ERCO", "summer").mean()
    for i in range(3):
        case = new_case(
            store,
            entity="ERCO",
            metric="forecast_error_mw",
            as_of=datetime(2023, 7, 20 + i),
            state="concluded",
        )
        close_case(
            store,
            case.case_id,
            verdicts={"H1_WEATHER_DEMAND": "confirmed", "H0_DATA_ARTIFACT": "refuted"},
        )
    after = effective_prior(store, "H1_WEATHER_DEMAND", "ERCO", "summer").mean()
    assert after > before
    assert effective_prior(store, "H0_DATA_ARTIFACT", "ERCO", "summer").mean() < 0.15


def test_6_llm_budget_and_cost_are_logged(store: duckdb.DuckDBPyConnection) -> None:
    llm = FakeLLM()
    case = investigate(store, llm, entity="ERCO", metric="forecast_error_mw", as_of=URI_AS_OF)
    assert case.llm_calls <= 4
    assert case.llm_usage.input_tokens >= 0 and case.llm_usage.cost_usd >= 0
    assert get_case(store, case.case_id).llm_calls == case.llm_calls


def test_7_plan_schema_matches_the_spec() -> None:
    p = InvestigationPlan(
        case_id="c",
        as_of=URI_AS_OF,
        hypotheses=["H0_DATA_ARTIFACT", "H2_SUPPLY_SHORTFALL"],
        steps=[
            ToolStep(
                step_id="s1",
                tool="identity.balance",
                inputs={},
                hypothesis_ids=["H0_DATA_ARTIFACT"],
            )
        ],
    )
    assert p.budget_steps == 12 and p.steps[0].depends_on == []


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("GRIDCASE_LIVE_LLM"), reason="set GRIDCASE_LIVE_LLM=1 with ANTHROPIC_API_KEY"
)
def test_8_uri_with_the_real_planner(store: duckdb.DuckDBPyConnection) -> None:
    from gridcase.planner.llm import AnthropicLLM

    llm = AnthropicLLM()
    case = investigate(store, llm, entity="ERCO", metric="forecast_error_mw", as_of=URI_AS_OF)
    assert case.plan is not None and case.plan_attempts <= 3
    assert case.conclusion is not None
    assert llm.calls <= 4
    print("LLM usage", case.llm_usage)
