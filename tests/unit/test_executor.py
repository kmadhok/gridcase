"""Executor: topological order, falsifiers after each step, skip steps whose drivers are all
refuted, and a failing tool fails only its own step."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from gridcase.planner.executor import execute_plan
from gridcase.planner.schema import InvestigationPlan, ToolStep
from gridcase.store.db import connect
from tests.polymarket_synth import add_market, add_trades

START = datetime(2026, 10, 1)
AS_OF = START + timedelta(days=3, hours=14, minutes=20)
W = (START + timedelta(days=3, hours=8), START + timedelta(days=3, hours=14))


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "e.duckdb")
    add_market(conn, "m1", "Sports", "England vs Czechia")
    rows = [
        {
            "ts": START + timedelta(hours=h, minutes=k),
            "wallet": f"w{k}",
            "usd": 100.0,
            "condition_id": "m1",
            "trade_id": f"{h}-{k}",
        }
        for h in range(3 * 24 + 15)
        for k in range(3)
    ]
    add_trades(conn, rows)
    yield conn
    conn.close()


def kpi_step(sid: str, tool: str, hyps: list[str], deps: list[str] | None = None) -> ToolStep:
    return ToolStep(
        step_id=sid,
        tool=tool,
        hypothesis_ids=hyps,
        depends_on=deps or [],
        inputs={"kpi": "volume_usd", "filters": {}, "window": list(W), "baseline_days": 3},
    )


def test_a_refuted_driver_skips_its_remaining_steps(db: duckdb.DuckDBPyConnection) -> None:
    # complete coverage refutes the data check; a later step that only serves it is skipped
    plan = InvestigationPlan(
        case_id="calm",
        as_of=AS_OF,
        hypotheses=["D0_DATA_PROBLEM", "D1_ONE_MARKET"],
        steps=[
            ToolStep(
                step_id="s1",
                tool="data.coverage",
                inputs={"window": list(W)},
                hypothesis_ids=["D0_DATA_PROBLEM"],
            ),
            ToolStep(
                step_id="s2",
                tool="data.coverage",
                inputs={"window": [W[0], W[1] - timedelta(hours=1)]},
                hypothesis_ids=["D0_DATA_PROBLEM"],
                depends_on=["s1"],
            ),
            kpi_step("s3", "by.market", ["D1_ONE_MARKET"], deps=["s1"]),
        ],
    )
    result = execute_plan(db, plan, case_id="calm")
    assert result.hypothesis_status["D0_DATA_PROBLEM"] == "refuted"
    assert result.step_status == {"s1": "ran", "s2": "skipped", "s3": "ran"}


def test_tool_failure_marks_step_failed_and_continues(db: duckdb.DuckDBPyConnection) -> None:
    bad = kpi_step("s1", "by.market", ["D1_ONE_MARKET"])
    bad.inputs["kpi"] = "revenue"
    plan = InvestigationPlan(
        case_id="fail",
        as_of=AS_OF,
        hypotheses=["D0_DATA_PROBLEM", "D1_ONE_MARKET"],
        steps=[
            bad,
            ToolStep(
                step_id="s2",
                tool="data.coverage",
                inputs={"window": list(W)},
                hypothesis_ids=["D0_DATA_PROBLEM"],
            ),
        ],
    )
    result = execute_plan(db, plan, case_id="fail")
    assert result.step_status == {"s1": "failed", "s2": "ran"}
    assert result.hypothesis_status["D1_ONE_MARKET"] == "open"  # untested, not refuted
    assert "unknown KPI" in result.errors["s1"]


def test_dependencies_run_before_dependents(db: duckdb.DuckDBPyConnection) -> None:
    plan = InvestigationPlan(
        case_id="order",
        as_of=AS_OF,
        hypotheses=["D0_DATA_PROBLEM", "D1_ONE_MARKET"],
        steps=[
            kpi_step("b", "by.market", ["D1_ONE_MARKET"], deps=["a"]),
            ToolStep(
                step_id="a",
                tool="data.coverage",
                inputs={"window": list(W)},
                hypothesis_ids=["D0_DATA_PROBLEM"],
            ),
        ],
    )
    assert execute_plan(db, plan, case_id="order").order == ["a", "b"]
