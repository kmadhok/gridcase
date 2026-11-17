"""An investigation failure is terminal for that case, and permits the next alarm."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from gridcase.market_alarm import WatchedSeries, check_series
from gridcase.market_watch import watch_markets
from gridcase.memory.cases import (
    IllegalTransition,
    advance,
    effective_prior,
    list_cases,
    new_case,
    similar_cases,
)
from gridcase.planner.llm import FakeLLM
from gridcase.server.app import _case_full, _case_summary
from gridcase.store.db import connect
from gridcase.trace import active_tracer
from tests.polymarket_synth import AS_OF, add_market, add_trades

investigation = importlib.import_module("gridcase.investigate")


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "store.duckdb")
    conn.execute("BEGIN TRANSACTION")
    add_market(conn, "c1", "Sports")
    rows: list[dict[str, Any]] = [
        {
            "ts": AS_OF.replace(hour=0, minute=0) - timedelta(days=3),
            "wallet": "w",
            "usd": 10.0,
        }
    ]
    for days in range(4):
        for hour in range(8, 15):
            rows.append(
                {
                    "ts": AS_OF.replace(hour=hour, minute=0) - timedelta(days=days),
                    "wallet": "w",
                    "usd": 100.0 if days else 1000.0,
                }
            )
    add_trades(conn, rows)
    conn.execute("COMMIT")
    yield conn
    conn.close()


def fail(*args: Any, **kwargs: Any) -> Any:
    raise duckdb.OutOfMemoryException("injected spill cap")


@pytest.mark.parametrize(
    "stage", ["on_open", "run_tool", "make_plan", "execute_plan", "explain", "publish_case"]
)
def test_an_escaping_exception_marks_the_case_failed_and_preserves_the_error(
    db: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    if stage != "on_open":
        monkeypatch.setattr(investigation, stage, fail)
    with pytest.raises(duckdb.OutOfMemoryException, match="injected spill cap"):
        investigation.investigate(
            db, FakeLLM(), "volume_usd", AS_OF, on_open=fail if stage == "on_open" else None
        )
    [case] = list_cases(db)
    assert case.state == "failed"
    assert active_tracer.get() is None
    assert similar_cases(db, case.entity, case.metric, case.season) == []
    assert db.execute("SELECT count(*) FROM learned_priors").fetchone() == (0,)
    assert _case_summary(case)["state"] == _case_full(case)["state"] == "failed"


def test_a_failed_compare_does_not_suppress_the_next_hours_alarm(
    db: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watched = [WatchedSeries(kpi="volume_usd", filters={"category": "Sports"})]
    assert check_series(db, watched[0], AS_OF).output["status"] == "alarm"
    with monkeypatch.context() as patch:
        patch.setattr(investigation, "run_tool", fail)
        assert watch_markets(db, AS_OF, FakeLLM, FakeLLM, 10, watched=watched) == []
    [failed] = list_cases(db)
    assert failed.state == "failed"
    opened = watch_markets(db, AS_OF + timedelta(hours=1), FakeLLM, FakeLLM, 10, watched=watched)
    assert len(opened) == 1 and opened[0] != failed.case_id
    assert {c.state for c in list_cases(db)} == {"failed", "action_taken"}


def test_failure_to_record_failed_state_keeps_the_original_exception(
    db: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(investigation, "run_tool", fail)

    def write_fails(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("state write failed")

    monkeypatch.setattr(investigation, "advance", write_fails)
    with pytest.raises(duckdb.OutOfMemoryException, match="injected spill cap"):
        investigation.investigate(db, FakeLLM(), "volume_usd", AS_OF)
    assert "state write failed" in caplog.text
    assert active_tracer.get() is None


@pytest.mark.parametrize("state", ["detected", "planned", "executing", "concluded", "action_taken"])
def test_failed_is_terminal_and_does_not_teach_priors(
    db: duckdb.DuckDBPyConnection,
    state: str,
) -> None:
    before = effective_prior(db, "D1_ONE_MARKET", "volume_usd", "all")
    case = new_case(db, "all", "volume_usd", AS_OF, state=state)
    advance(db, case.case_id, "failed")
    with pytest.raises(IllegalTransition):
        advance(db, case.case_id, "planned")
    assert effective_prior(db, "D1_ONE_MARKET", "volume_usd", "all") == before
