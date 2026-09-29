"""Plan validator: pure rules, one error code per rule."""

from __future__ import annotations

from datetime import datetime

from gridcase.planner.schema import InvestigationPlan, ToolStep
from gridcase.planner.validate import validate_plan

AS_OF = datetime(2021, 2, 16, 12)
W = [datetime(2021, 2, 14), datetime(2021, 2, 16)]


def step(
    sid: str, tool: str, hyps: list[str], deps: list[str] | None = None, **inputs: object
) -> ToolStep:
    base: dict[str, object] = {"entity": "ERCO", "window": W}
    base.update(inputs)
    return ToolStep(step_id=sid, tool=tool, inputs=base, hypothesis_ids=hyps, depends_on=deps or [])


def plan(hyps: list[str], steps: list[ToolStep], budget: int = 12) -> InvestigationPlan:
    return InvestigationPlan(
        case_id="c", as_of=AS_OF, hypotheses=hyps, steps=steps, budget_steps=budget
    )


GOOD = plan(
    ["H0_DATA_ARTIFACT", "H5_SUBREGION_CONCENTRATION"],
    [
        step("s1", "identity.balance", ["H0_DATA_ARTIFACT"]),
        step("s2", "anomaly.detect", ["H0_DATA_ARTIFACT"], metric="demand"),
        step("s3", "revision.diff", ["H0_DATA_ARTIFACT"], series="eia.D", t_a=W[0], t_b=W[1]),
        step("s4", "contribution.subregion", ["H5_SUBREGION_CONCENTRATION"], deps=["s1"]),
    ],
)


def test_good_plan_has_no_errors() -> None:
    assert validate_plan(GOOD) == []


def test_h0_must_be_present_and_first() -> None:
    assert {
        e.code
        for e in validate_plan(
            GOOD.model_copy(update={"hypotheses": ["H5_SUBREGION_CONCENTRATION"]})
        )
    } >= {"E_NO_H0"}
    swapped = GOOD.model_copy(
        update={"hypotheses": ["H5_SUBREGION_CONCENTRATION", "H0_DATA_ARTIFACT"]}
    )
    assert "E_H0_NOT_FIRST" in {e.code for e in validate_plan(swapped)}


def test_at_least_two_hypotheses() -> None:
    p = GOOD.model_copy(update={"hypotheses": ["H0_DATA_ARTIFACT"], "steps": GOOD.steps[:3]})
    assert "E_TOO_FEW_HYP" in {e.code for e in validate_plan(p)}


def test_unknown_tool_hypothesis_and_entity() -> None:
    p = plan(GOOD.hypotheses, [*GOOD.steps, step("s9", "nope.tool", ["H0_DATA_ARTIFACT"])])
    assert "E_UNKNOWN_TOOL" in {e.code for e in validate_plan(p)}
    p = plan([*GOOD.hypotheses, "H9_GHOST"], GOOD.steps)
    assert "E_UNKNOWN_HYPOTHESIS" in {e.code for e in validate_plan(p)}
    p = plan(
        GOOD.hypotheses,
        [
            *GOOD.steps[:3],
            step("s4", "contribution.subregion", ["H5_SUBREGION_CONCENTRATION"], entity="MARS"),
        ],
    )
    assert "E_UNKNOWN_ENTITY" in {e.code for e in validate_plan(p)}


def test_dag_must_be_acyclic_and_dependencies_must_exist() -> None:
    s = [x.model_copy() for x in GOOD.steps]
    s[0] = s[0].model_copy(update={"depends_on": ["s4"]})
    assert "E_CYCLE" in {e.code for e in validate_plan(plan(GOOD.hypotheses, s))}
    s = [x.model_copy() for x in GOOD.steps]
    s[0] = s[0].model_copy(update={"depends_on": ["s404"]})
    assert "E_BAD_DEPENDENCY" in {e.code for e in validate_plan(plan(GOOD.hypotheses, s))}


def test_budget_and_duplicate_step_ids() -> None:
    assert "E_OVER_BUDGET" in {
        e.code for e in validate_plan(plan(GOOD.hypotheses, GOOD.steps, budget=3))
    }
    dup = plan(GOOD.hypotheses, [*GOOD.steps, GOOD.steps[0]])
    assert "E_DUP_STEP" in {e.code for e in validate_plan(dup)}


def test_every_hypothesis_needs_a_step_that_can_falsify_it() -> None:
    # H5's falsifier reads contribution.subregion; peer.compare alone cannot refute it
    p = plan(
        GOOD.hypotheses,
        [
            *GOOD.steps[:3],
            step("s4", "peer.compare", ["H5_SUBREGION_CONCENTRATION"], peers=["PJM"]),
        ],
    )
    errs = validate_plan(p)
    assert any(e.code == "E_UNFALSIFIABLE" and "H5" in e.message for e in errs)


def test_step_hypotheses_must_be_in_the_plan() -> None:
    p = plan(
        GOOD.hypotheses,
        [*GOOD.steps, step("s5", "bridge.fuel", ["H2_SUPPLY_SHORTFALL"], t0=W[0], t1=W[1])],
    )
    assert "E_STEP_HYPOTHESIS" in {e.code for e in validate_plan(p)}


def test_step_inputs_must_validate_against_the_tools_input_model() -> None:
    """An empty or mistyped inputs object is a plan error the planner can be told about."""
    bad = plan(
        ["H0_DATA_ARTIFACT", "H5_SUBREGION_CONCENTRATION"],
        [
            ToolStep(
                step_id="s1",
                tool="identity.balance",
                inputs={},
                hypothesis_ids=["H0_DATA_ARTIFACT"],
            ),
            step("s2", "anomaly.detect", ["H0_DATA_ARTIFACT"], metric="demand"),
            step("s3", "revision.diff", ["H0_DATA_ARTIFACT"], series="eia.D", t_a=W[0], t_b=W[1]),
            step("s4", "contribution.subregion", ["H5_SUBREGION_CONCENTRATION"], deps=["s1"]),
        ],
    )
    errors = validate_plan(bad)
    codes = {(e.code, e.step_id) for e in errors}
    assert ("E_BAD_INPUTS", "s1") in codes
    msg = next(e.message for e in errors if e.code == "E_BAD_INPUTS")
    assert "entity" in msg and "window" in msg
    assert not [e for e in validate_plan(GOOD) if e.code == "E_BAD_INPUTS"]
