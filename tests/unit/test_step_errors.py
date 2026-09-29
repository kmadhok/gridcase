"""A step that fails at run time must be visible on the case, not silently absent."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from gridcase.bench.inject import build_scratch
from gridcase.investigate import investigate
from gridcase.memory.cases import get_case
from gridcase.planner.llm import FakeLLM, default_plan_response
from gridcase.planner.schema import ToolStep
from gridcase.store.db import connect
from tests.conftest import FIXTURE_PARQUET

AS_OF = datetime(2021, 2, 16, 12)


def test_failed_steps_are_persisted_and_reported_as_a_caveat(tmp_path: Path) -> None:
    db = tmp_path / "s.duckdb"
    build_scratch(FIXTURE_PARQUET, db)
    plan = default_plan_response("c", "ERCO", AS_OF)
    # Valid for the validator, doomed at run time: an anomaly step over a window with no data.
    doomed = ToolStep(
        step_id="s_doomed",
        tool="anomaly.detect",
        inputs={
            "metric": "demand",
            "entity": "ERCO",
            "window": (datetime(2015, 1, 1), datetime(2015, 1, 3)),
        },
        hypothesis_ids=["H2_SUPPLY_SHORTFALL"],  # H0 is refuted early and its steps skipped
    )
    # First in the DAG: it runs before any falsifier can refute its hypothesis and skip it.
    plan = plan.model_copy(update={"steps": [doomed, *plan.steps], "budget_steps": 13})
    conn = connect(db)
    case = investigate(conn, FakeLLM(plans=[plan]), "ERCO", "forecast_error_mw", AS_OF)
    assert case.plan is not None and "s_doomed" in {s.step_id for s in case.plan.steps}
    assert "s_doomed" in case.step_errors, case.step_errors
    assert case.conclusion is not None
    assert any("s_doomed" in c and "anomaly.detect" in c for c in case.conclusion.caveats)
    again = get_case(conn, case.case_id)
    assert again.step_errors == case.step_errors
    conn.close()
