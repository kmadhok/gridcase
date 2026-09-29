"""Plan validator: every rule has its own error code, and all violations are reported."""

from __future__ import annotations

from datetime import datetime

from gridcase.planner.schema import InvestigationPlan, ToolStep
from gridcase.planner.validate import validate_plan

AS_OF = datetime(2026, 10, 5, 14)
W = [datetime(2026, 10, 5, 8), datetime(2026, 10, 5, 14)]


def step(
    sid: str, tool: str, hyps: list[str], deps: list[str] | None = None, **inputs: object
) -> ToolStep:
    base: dict[str, object] = (
        {"window": W}
        if tool == "data.coverage"
        else {"kpi": "volume_usd", "filters": {}, "window": W, "baseline_days": 7}
    )
    base.update(inputs)
    return ToolStep(step_id=sid, tool=tool, inputs=base, hypothesis_ids=hyps, depends_on=deps or [])


def plan(hyps: list[str], steps: list[ToolStep], budget: int = 12) -> InvestigationPlan:
    return InvestigationPlan(
        case_id="c", as_of=AS_OF, hypotheses=hyps, steps=steps, budget_steps=budget
    )


GOOD = plan(
    ["D0_DATA_PROBLEM", "D1_ONE_MARKET", "D6_BROAD_BASED"],
    [
        step("s1", "data.coverage", ["D0_DATA_PROBLEM"]),
        step("s2", "by.market", ["D1_ONE_MARKET", "D6_BROAD_BASED"], deps=["s1"]),
    ],
)


def codes(p: InvestigationPlan) -> set[str]:
    return {e.code for e in validate_plan(p)}


def test_good_plan_has_no_errors() -> None:
    assert validate_plan(GOOD) == []


def test_the_data_check_must_be_present_and_first() -> None:
    missing = plan(
        ["D1_ONE_MARKET", "D6_BROAD_BASED"],
        [step("s1", "by.market", ["D1_ONE_MARKET", "D6_BROAD_BASED"])],
    )
    assert "E_NO_DATA_CHECK" in codes(missing)
    late = plan(
        ["D1_ONE_MARKET", "D0_DATA_PROBLEM"],
        [
            step("s1", "by.market", ["D1_ONE_MARKET"]),
            step("s2", "data.coverage", ["D0_DATA_PROBLEM"]),
        ],
    )
    assert "E_DATA_CHECK_NOT_FIRST" in codes(late)


def test_at_least_two_drivers() -> None:
    assert "E_TOO_FEW_HYP" in codes(
        plan(["D0_DATA_PROBLEM"], [step("s1", "data.coverage", ["D0_DATA_PROBLEM"])])
    )


def test_unknown_tool_driver_kpi_and_segment() -> None:
    p = plan(
        ["D0_DATA_PROBLEM", "D9_MOON_PHASE"],
        [
            step("s1", "data.coverage", ["D0_DATA_PROBLEM"]),
            ToolStep(step_id="s2", tool="astro.moon", inputs={}, hypothesis_ids=["D9_MOON_PHASE"]),
            step("s3", "by.market", ["D0_DATA_PROBLEM"], kpi="revenue"),
            step("s4", "by.market", ["D0_DATA_PROBLEM"], filters={"planet": "Mars"}),
            step("s5", "by.market", ["D0_DATA_PROBLEM"], filters={"size_band": "huge"}),
        ],
    )
    got = validate_plan(p)
    assert {"E_UNKNOWN_TOOL", "E_UNKNOWN_HYPOTHESIS", "E_UNKNOWN_SEGMENT"} <= {e.code for e in got}
    assert sum(e.code == "E_UNKNOWN_SEGMENT" for e in got) == 3


def test_dag_must_be_acyclic_and_dependencies_must_exist() -> None:
    cyc = plan(
        ["D0_DATA_PROBLEM", "D1_ONE_MARKET"],
        [
            step("s1", "data.coverage", ["D0_DATA_PROBLEM"], deps=["s2"]),
            step("s2", "by.market", ["D1_ONE_MARKET"], deps=["s1"]),
            step("s3", "by.size", ["D1_ONE_MARKET"], deps=["s9"]),
        ],
    )
    assert {"E_CYCLE", "E_BAD_DEPENDENCY"} <= codes(cyc)


def test_budget_and_duplicate_step_ids() -> None:
    p = plan(
        ["D0_DATA_PROBLEM", "D1_ONE_MARKET"],
        [
            step("s1", "data.coverage", ["D0_DATA_PROBLEM"]),
            step("s1", "by.market", ["D1_ONE_MARKET"]),
        ],
        budget=1,
    )
    assert {"E_OVER_BUDGET", "E_DUP_STEP"} <= codes(p)


def test_every_driver_needs_a_step_that_can_falsify_it() -> None:
    p = plan(
        ["D0_DATA_PROBLEM", "D4_WHALES"],
        [step("s1", "data.coverage", ["D0_DATA_PROBLEM"]), step("s2", "by.market", ["D4_WHALES"])],
    )
    errs = [e for e in validate_plan(p) if e.code == "E_UNFALSIFIABLE"]
    assert len(errs) == 1 and "by.size" in errs[0].message


def test_step_drivers_must_be_in_the_plan() -> None:
    p = plan(
        ["D0_DATA_PROBLEM", "D1_ONE_MARKET"],
        [
            step("s1", "data.coverage", ["D0_DATA_PROBLEM"]),
            step("s2", "by.market", ["D1_ONE_MARKET", "D4_WHALES"]),
        ],
    )
    assert "E_STEP_HYPOTHESIS" in codes(p)


def test_step_inputs_must_validate_against_the_tools_input_model() -> None:
    bad = plan(
        ["D0_DATA_PROBLEM", "D1_ONE_MARKET"],
        [
            ToolStep(
                step_id="s1", tool="data.coverage", inputs={}, hypothesis_ids=["D0_DATA_PROBLEM"]
            ),
            step("s2", "by.market", ["D1_ONE_MARKET"]),
        ],
    )
    errors = validate_plan(bad)
    assert [(e.code, e.step_id) for e in errors if e.code == "E_BAD_INPUTS"] == [
        ("E_BAD_INPUTS", "s1")
    ]
    assert "window" in next(e.message for e in errors if e.code == "E_BAD_INPUTS")
