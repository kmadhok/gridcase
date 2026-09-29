"""Executor: topological order, falsifiers after each step, skip steps for refuted hypotheses."""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from gridcase.planner.executor import execute_plan
from gridcase.planner.schema import InvestigationPlan, ToolStep

AS_OF = datetime(2023, 5, 10)
W = (datetime(2023, 5, 1), datetime(2023, 5, 8))


def test_refuted_hypothesis_skips_its_remaining_steps(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    # On a calm week H0's falsifier is TRUE (no artifact) after its three tests, and H5's
    # contribution step then runs; a step that only serves H0 after refutation is skipped.
    plan = InvestigationPlan(
        case_id="calm",
        as_of=AS_OF,
        hypotheses=["H0_DATA_ARTIFACT", "H5_SUBREGION_CONCENTRATION"],
        steps=[
            ToolStep(
                step_id="s1",
                tool="identity.balance",
                inputs={"entity": "ERCO", "window": W},
                hypothesis_ids=["H0_DATA_ARTIFACT"],
            ),
            ToolStep(
                step_id="s2",
                tool="anomaly.detect",
                inputs={"metric": "demand", "entity": "ERCO", "window": W},
                hypothesis_ids=["H0_DATA_ARTIFACT"],
            ),
            ToolStep(
                step_id="s3",
                tool="revision.diff",
                inputs={
                    "series": "eia.D",
                    "entity": "ERCO",
                    "t_a": AS_OF - timedelta(days=2),
                    "t_b": AS_OF,
                },
                hypothesis_ids=["H0_DATA_ARTIFACT"],
            ),
            ToolStep(
                step_id="s4",
                tool="variance.plan_vs_actual",
                inputs={"entity": "ERCO", "window": W},
                hypothesis_ids=["H0_DATA_ARTIFACT"],
                depends_on=["s3"],
            ),
            ToolStep(
                step_id="s5",
                tool="contribution.subregion",
                inputs={"entity": "ERCO", "window": W},
                hypothesis_ids=["H5_SUBREGION_CONCENTRATION"],
                depends_on=["s1"],
            ),
        ],
    )
    result = execute_plan(fixture_store, plan, case_id="calm")
    assert result.hypothesis_status["H0_DATA_ARTIFACT"] == "refuted"
    assert result.step_status == {
        "s1": "ran",
        "s2": "ran",
        "s3": "ran",
        "s4": "skipped",
        "s5": "ran",
    }
    assert len(result.receipts) == 4
    assert result.outputs["identity.balance"]["max_abs_z"] < 3


def test_tool_failure_marks_step_failed_and_continues(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    plan = InvestigationPlan(
        case_id="fail",
        as_of=AS_OF,
        hypotheses=["H0_DATA_ARTIFACT", "H2_SUPPLY_SHORTFALL"],
        steps=[
            ToolStep(
                step_id="s1",
                tool="bridge.fuel",
                inputs={"entity": "ERCO", "t0": datetime(1999, 1, 1), "t1": datetime(1999, 1, 2)},
                hypothesis_ids=["H2_SUPPLY_SHORTFALL"],
            ),
            ToolStep(
                step_id="s2",
                tool="identity.balance",
                inputs={"entity": "ERCO", "window": W},
                hypothesis_ids=["H0_DATA_ARTIFACT"],
            ),
        ],
    )
    result = execute_plan(fixture_store, plan, case_id="fail")
    assert result.step_status == {"s1": "failed", "s2": "ran"}
    assert result.hypothesis_status["H2_SUPPLY_SHORTFALL"] == "open"  # untested, not refuted
    assert result.hypothesis_status["H0_DATA_ARTIFACT"] == "open"  # H0 needs three tools
    assert "no fuel data" in result.errors["s1"]


def test_dependencies_run_before_dependents(fixture_store: duckdb.DuckDBPyConnection) -> None:
    plan = InvestigationPlan(
        case_id="order",
        as_of=AS_OF,
        hypotheses=["H0_DATA_ARTIFACT", "H5_SUBREGION_CONCENTRATION"],
        steps=[
            ToolStep(
                step_id="b",
                tool="contribution.subregion",
                inputs={"entity": "ERCO", "window": W},
                hypothesis_ids=["H5_SUBREGION_CONCENTRATION"],
                depends_on=["a"],
            ),
            ToolStep(
                step_id="a",
                tool="identity.balance",
                inputs={"entity": "ERCO", "window": W},
                hypothesis_ids=["H0_DATA_ARTIFACT"],
            ),
        ],
    )
    result = execute_plan(fixture_store, plan, case_id="order")
    assert result.order == ["a", "b"]
