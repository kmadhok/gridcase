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
    CaseRecord,
    IllegalTransition,
    advance,
    close_case,
    effective_prior,
    get_case,
    list_cases,
    new_case,
    save_case,
    similar_cases,
)
from gridcase.planner.context import PlanContext
from gridcase.planner.llm import FakeLLM, LLMUsage
from gridcase.planner.schema import InvestigationPlan
from gridcase.server.app import _case_full, _case_summary
from gridcase.server.scheduler import drain_scheduled
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


@pytest.mark.parametrize("stage", ["on_open", "run_tool", "make_plan", "execute_plan", "explain"])
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


@pytest.mark.parametrize("state", ["detected", "planned", "executing"])
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


class PaidLLM(FakeLLM):
    def plan(
        self, ctx: PlanContext, feedback: list[str]
    ) -> tuple[InvestigationPlan | None, LLMUsage]:
        plan, _ = super().plan(ctx, feedback)
        return plan, LLMUsage(cost_usd=0.1, input_tokens=100, model="test")


def test_failed_investigation_saves_usage_not_yet_persisted(
    db: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = investigation.save_case
    writes = 0

    def fail_first(conn: duckdb.DuckDBPyConnection, case: CaseRecord) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise RuntimeError("first usage save failed")
        original(conn, case)

    monkeypatch.setattr(investigation, "save_case", fail_first)
    with pytest.raises(RuntimeError, match="first usage save failed"):
        investigation.investigate(db, PaidLLM(), "volume_usd", AS_OF)
    [case] = list_cases(db)
    assert case.llm_usage == LLMUsage(cost_usd=0.1, input_tokens=100, model="test")
    assert case.state == "failed"


@pytest.mark.parametrize("after_plan", [False, True])
def test_failed_attempt_uses_daily_paid_slot(
    db: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch, after_plan: bool
) -> None:
    # Freeze case opening dates too: the watch is running against a historical fixture.
    monkeypatch.setattr("gridcase.memory.cases._now", lambda: AS_OF)
    monkeypatch.setattr(investigation, "execute_plan" if after_plan else "make_plan", fail)
    used: list[str] = []

    def paid() -> FakeLLM:
        used.append("paid")
        return PaidLLM()

    def free() -> FakeLLM:
        used.append("free")
        return FakeLLM()

    watched = [WatchedSeries(kpi="volume_usd", filters={"category": "Sports"})]
    for offset in range(2):
        assert (
            watch_markets(db, AS_OF + timedelta(minutes=offset), paid, free, 1, watched=watched)
            == []
        )
    assert len(list_cases(db)) == 2
    assert used == ["paid", "free"]


def test_two_failures_in_window_suppress_a_third_attempt(
    db: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(investigation, "run_tool", fail)
    series = WatchedSeries(kpi="volume_usd", filters={"category": "Sports"})
    # Failures outside the window or for another series must not consume its retry limit.
    new_case(db, series.segment, series.kpi, AS_OF - timedelta(hours=25), state="failed")
    new_case(db, "all", series.kpi, AS_OF, state="failed")
    new_case(db, series.segment, "active_traders", AS_OF, state="failed")
    for offset in range(3):
        assert (
            watch_markets(
                db, AS_OF + timedelta(minutes=offset), FakeLLM, FakeLLM, 10, watched=[series]
            )
            == []
        )
    recent = [
        c
        for c in list_cases(db)
        if c.entity == series.segment and c.metric == series.kpi and c.as_of >= AS_OF
    ]
    assert len(recent) == 2


@pytest.mark.parametrize("state", ["concluded", "action_taken"])
def test_concluded_cases_cannot_fail(db: duckdb.DuckDBPyConnection, state: str) -> None:
    case = new_case(db, "all", "volume_usd", AS_OF, state=state)
    with pytest.raises(IllegalTransition):
        advance(db, case.case_id, "failed")


def test_exception_after_publish_keeps_recheck_working(
    db: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    publish = investigation.publish_case

    def publish_then_fail(*args: Any, **kwargs: Any) -> Any:
        publish(*args, **kwargs)
        raise RuntimeError("after publish")

    monkeypatch.setattr(investigation, "publish_case", publish_then_fail)
    with pytest.raises(RuntimeError, match="after publish"):
        investigation.investigate(db, FakeLLM(), "volume_usd", AS_OF)
    [case] = list_cases(db)
    assert case.state == "concluded"
    assert "could not mark case" not in caplog.text
    assert drain_scheduled(db, AS_OF + timedelta(days=1, minutes=1))
    assert get_case(db, case.case_id).state == "closed"


@pytest.mark.parametrize("state", ["failed", "closed"])
def test_terminal_case_with_pending_recheck_does_nothing(
    db: duckdb.DuckDBPyConnection, state: str
) -> None:
    case = investigation.investigate(db, FakeLLM(), "volume_usd", AS_OF)
    # A legacy failed case can already have both a conclusion and a queued recheck.
    case.state = state
    save_case(db, case)
    before = get_case(db, case.case_id)
    assert drain_scheduled(db, AS_OF + timedelta(days=1, minutes=1))
    assert get_case(db, case.case_id) == before
    assert db.execute("SELECT count(*) FROM learned_priors").fetchone() == (0,)


def test_close_case_refuses_failed_case(db: duckdb.DuckDBPyConnection) -> None:
    case = new_case(db, "all", "volume_usd", AS_OF, state="failed")
    with pytest.raises(IllegalTransition):
        close_case(db, case.case_id, {"D1_ONE_MARKET": "confirmed"})
    assert get_case(db, case.case_id) == case
    assert db.execute("SELECT count(*) FROM learned_priors").fetchone() == (0,)
