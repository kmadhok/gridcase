"""Planner mechanics: placeholder filling, retry/fallback, FakeLLM, executor skip logic,
confidence and prior updates."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
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

AS_OF = datetime(2026, 10, 5, 14)
WINDOW = (datetime(2026, 10, 5, 8), AS_OF)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "p.duckdb")
    yield conn
    conn.close()


def ctx() -> PlanContext:
    return PlanContext(
        case_id="c1",
        kpi="volume_usd",
        filters={"category": "Sports"},
        as_of=AS_OF,
        window=WINDOW,
        baseline_days=7,
        record_days=30,
        applicable=["D0_DATA_PROBLEM", "D1_ONE_MARKET", "D4_WHALES", "D6_BROAD_BASED"],
        priors={
            "D0_DATA_PROBLEM": 0.1,
            "D1_ONE_MARKET": 0.4,
            "D4_WHALES": 0.3,
            "D6_BROAD_BASED": 0.3,
        },
    )


def test_fill_inputs_substitutes_every_placeholder() -> None:
    filled = fill_inputs(
        {
            "kpi": "{kpi}",
            "filters": "{filters}",
            "window": "{window}",
            "baseline_days": "{baseline_days}",
            "as_of": "{as_of}",
            "top_n": 3,
        },
        ctx(),
    )
    assert filled == {
        "kpi": "volume_usd",
        "filters": {"category": "Sports"},
        "window": [WINDOW[0], WINDOW[1]],
        "baseline_days": 7,
        "as_of": AS_OF,
        "top_n": 3,
    }


def test_drivers_that_do_not_apply_are_left_out() -> None:
    from gridcase.planner.context import applies
    from gridcase.planner.hypotheses import template_by_id

    assert not applies(template_by_id("D2_ONE_CATEGORY"), {"category": "Sports"}, 30)
    assert applies(template_by_id("D2_ONE_CATEGORY"), {}, 30)
    assert not applies(template_by_id("D3_NEW_TRADERS"), {}, 3)  # "new" needs a week of record


def test_fake_llm_returns_a_valid_plan_first_time() -> None:
    plan, attempts, usage = make_plan(FakeLLM(), ctx())
    assert attempts == 1 and plan.hypotheses[0] == "D0_DATA_PROBLEM"
    assert isinstance(usage, LLMUsage)


def test_planner_retries_on_invalid_plans_then_falls_back_to_template_order() -> None:
    bad = InvestigationPlan(case_id="c1", as_of=AS_OF, hypotheses=["D1_ONE_MARKET"], steps=[])
    llm = FakeLLM(plans=[bad, bad, bad])
    plan, attempts, _ = make_plan(llm, ctx())
    assert attempts == 3 and llm.calls == 3
    assert plan.hypotheses[0] == "D0_DATA_PROBLEM" and len(plan.hypotheses) >= 2
    assert plan.source == "template_default"


def test_planner_accepts_a_valid_plan_on_the_second_try() -> None:
    bad = InvestigationPlan(case_id="c1", as_of=AS_OF, hypotheses=["D1_ONE_MARKET"], steps=[])
    llm = FakeLLM(plans=[bad])  # then falls through to its default valid plan
    plan, attempts, _ = make_plan(llm, ctx())
    assert attempts == 2 and plan.source == "llm"


def test_confidence_moves_with_margin_and_prior() -> None:
    p = Prior(a=3.5, b=6.5)
    assert confidence(p, margin=None) == pytest.approx(p.mean(), abs=1e-9)
    assert confidence(p, margin=1.0) > p.mean() > confidence(p, margin=-1.0)
    assert 0.0 < confidence(Prior(a=0.5, b=9.5), margin=-3.0) < 0.05


def test_learned_priors_overlay_the_yaml_defaults(db: duckdb.DuckDBPyConnection) -> None:
    base = effective_prior(db, "D1_ONE_MARKET", "volume_usd", "all")
    update_learned_priors(
        db, "volume_usd", "all", {"D1_ONE_MARKET": "confirmed", "D0_DATA_PROBLEM": "refuted"}
    )
    after = effective_prior(db, "D1_ONE_MARKET", "volume_usd", "all")
    assert after.a == base.a + 1 and after.b == base.b
    assert effective_prior(db, "D0_DATA_PROBLEM", "volume_usd", "all").b == 9.0 + 1
    # other KPIs untouched
    assert effective_prior(db, "D1_ONE_MARKET", "active_traders", "all") == Prior(a=4.0, b=6.0)


def test_case_lifecycle_and_similar_cases(db: duckdb.DuckDBPyConnection) -> None:
    c1 = new_case(
        db, entity="all", metric="volume_usd", as_of=datetime(2026, 10, 3), state="concluded"
    )
    close_case(db, c1.case_id, verdicts={"D1_ONE_MARKET": "confirmed"})
    c2 = new_case(
        db, entity="all", metric="volume_usd", as_of=datetime(2026, 10, 2), state="concluded"
    )
    close_case(db, c2.case_id, verdicts={"D4_WHALES": "confirmed"})
    new_case(
        db,
        entity="category=Sports",
        metric="volume_usd",
        as_of=datetime(2026, 10, 4),
        state="detected",
    )
    sim = similar_cases(db, entity="all", metric="volume_usd", season="fall", k=3)
    assert [s.case_id for s in sim] == [c1.case_id, c2.case_id]
    assert sim[0].top_hypothesis == "D1_ONE_MARKET"


def test_state_machine_rejects_illegal_transitions(db: duckdb.DuckDBPyConnection) -> None:
    from gridcase.memory.cases import IllegalTransition, advance

    c = new_case(db, entity="all", metric="volume_usd", as_of=AS_OF, state="detected")
    advance(db, c.case_id, "planned")
    with pytest.raises(IllegalTransition):
        advance(db, c.case_id, "closed")
