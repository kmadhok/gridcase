"""Planner mechanics: placeholder filling, retry/fallback, FakeLLM, executor skip logic,
confidence and prior updates."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from gridcase.explain.confidence import confidence
from gridcase.memory.cases import (
    close_case,
    effective_prior,
    new_case,
    similar_cases,
    update_learned_priors,
)
from gridcase.planner.context import PlanContext, fill_inputs
from gridcase.planner.hypotheses import Prior
from gridcase.planner.llm import FakeLLM, LLMUsage
from gridcase.planner.planner import make_plan
from gridcase.planner.schema import InvestigationPlan
from gridcase.store.db import connect

AS_OF = datetime(2021, 2, 16, 12)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "p.duckdb")
    yield conn
    conn.close()


def ctx() -> PlanContext:
    return PlanContext(
        case_id="c1",
        entity="ERCO",
        metric="forecast_error_mw",
        as_of=AS_OF,
        window=(AS_OF - timedelta(hours=72), AS_OF),
        t0=datetime(2021, 2, 14, 18),
        t1=datetime(2021, 2, 15, 18),
        event_start=datetime(2021, 2, 15, 7),
        peers=["CISO", "PJM"],
        hub="ERCO/HB_HUBAVG",
        season="winter",
        anomaly={"max_abs_z": 9.0, "flagged": True},
        applicable=["H0_DATA_ARTIFACT", "H2_SUPPLY_SHORTFALL", "H4_LOAD_SHED"],
        priors={"H0_DATA_ARTIFACT": 0.15, "H2_SUPPLY_SHORTFALL": 0.35, "H4_LOAD_SHED": 0.1},
        similar_cases=[],
    )


def test_fill_inputs_substitutes_every_placeholder() -> None:
    filled = fill_inputs(
        {
            "entity": "{entity}",
            "window": "{window}",
            "t0": "{t0}",
            "t1": "{t1}",
            "peers": "{peers}",
            "hub": "{hub}",
            "series": "eia.D",
            "t_a": "{as_of_minus_48h}",
            "t_b": "{as_of}",
            "metric": "{metric}",
        },
        ctx(),
    )
    assert filled["entity"] == "ERCO" and filled["hub"] == "ERCO/HB_HUBAVG"
    assert filled["window"] == [AS_OF - timedelta(hours=72), AS_OF]
    assert filled["t_a"] == AS_OF - timedelta(hours=48) and filled["t_b"] == AS_OF
    assert filled["peers"] == ["CISO", "PJM"] and filled["metric"] == "forecast_error_mw"


def test_fake_llm_returns_a_valid_plan_first_time() -> None:
    plan, attempts, usage = make_plan(FakeLLM(), ctx())
    assert attempts == 1 and plan.hypotheses[0] == "H0_DATA_ARTIFACT"
    assert isinstance(usage, LLMUsage)


def test_planner_retries_on_invalid_plans_then_falls_back_to_template_order() -> None:
    bad = InvestigationPlan(case_id="c1", as_of=AS_OF, hypotheses=["H2_SUPPLY_SHORTFALL"], steps=[])
    llm = FakeLLM(plans=[bad, bad, bad])
    plan, attempts, _ = make_plan(llm, ctx())
    assert attempts == 3 and llm.calls == 3
    assert plan.hypotheses[0] == "H0_DATA_ARTIFACT" and len(plan.hypotheses) >= 2
    assert plan.source == "template_default"


def test_planner_accepts_a_valid_plan_on_the_second_try() -> None:
    bad = InvestigationPlan(case_id="c1", as_of=AS_OF, hypotheses=["H2_SUPPLY_SHORTFALL"], steps=[])
    llm = FakeLLM(plans=[bad])  # then falls through to its default valid plan
    plan, attempts, _ = make_plan(llm, ctx())
    assert attempts == 2 and plan.source == "llm"


def test_confidence_moves_with_margin_and_prior() -> None:
    p = Prior(a=3.5, b=6.5)
    assert confidence(p, margin=None) == pytest.approx(p.mean(), abs=1e-9)
    assert confidence(p, margin=1.0) > p.mean() > confidence(p, margin=-1.0)
    assert 0.0 < confidence(Prior(a=0.5, b=9.5), margin=-3.0) < 0.05


def test_learned_priors_overlay_the_yaml_defaults(db: duckdb.DuckDBPyConnection) -> None:
    base = effective_prior(db, "H1_WEATHER_DEMAND", "ERCO", "summer")
    update_learned_priors(
        db, "ERCO", "summer", {"H1_WEATHER_DEMAND": "confirmed", "H0_DATA_ARTIFACT": "refuted"}
    )
    after = effective_prior(db, "H1_WEATHER_DEMAND", "ERCO", "summer")
    assert after.a == base.a + 1 and after.b == base.b
    h0 = effective_prior(db, "H0_DATA_ARTIFACT", "ERCO", "summer")
    assert h0.b == 8.5 + 1
    # other seasons/BAs untouched
    assert effective_prior(db, "H1_WEATHER_DEMAND", "ERCO", "winter") == Prior(a=4.0, b=6.0)


def test_case_lifecycle_and_similar_cases(db: duckdb.DuckDBPyConnection) -> None:
    c1 = new_case(
        db,
        entity="ERCO",
        metric="forecast_error_mw",
        as_of=datetime(2023, 7, 20),
        state="concluded",
    )
    close_case(db, c1.case_id, verdicts={"H1_WEATHER_DEMAND": "confirmed"})
    c2 = new_case(
        db,
        entity="ERCO",
        metric="forecast_error_mw",
        as_of=datetime(2022, 7, 20),
        state="concluded",
    )
    close_case(db, c2.case_id, verdicts={"H2_SUPPLY_SHORTFALL": "confirmed"})
    new_case(
        db, entity="PJM", metric="forecast_error_mw", as_of=datetime(2023, 7, 21), state="detected"
    )
    sim = similar_cases(db, entity="ERCO", metric="forecast_error_mw", season="summer", k=3)
    assert [s.case_id for s in sim] == [c1.case_id, c2.case_id]
    assert sim[0].top_hypothesis == "H1_WEATHER_DEMAND"


def test_state_machine_rejects_illegal_transitions(db: duckdb.DuckDBPyConnection) -> None:
    from gridcase.memory.cases import IllegalTransition, advance

    c = new_case(db, entity="ERCO", metric="forecast_error_mw", as_of=AS_OF, state="detected")
    advance(db, c.case_id, "planned")
    with pytest.raises(IllegalTransition):
        advance(db, c.case_id, "closed")
