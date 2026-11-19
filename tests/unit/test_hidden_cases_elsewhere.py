"""Retracted cases and cases judged on missing data (the record far behind when they opened) are
left out of the digest's alarms card and the Now question box's latest investigations, as they
are from the Now page."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from gridcase.ask.context import now_context
from gridcase.digest import build_digest
from gridcase.ledger.schema import Receipt
from gridcase.ledger.store import write_receipt
from gridcase.memory.cases import CaseRecord, list_cases, new_case
from gridcase.memory.retractions import retract_case
from gridcase.planner.llm import FakeLLM
from gridcase.server.app import create_app, now_cases
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF


def _coverage(conn: duckdb.DuckDBPyConnection, case: CaseRecord, freshness: float) -> None:
    write_receipt(
        conn,
        case.case_id,
        Receipt.build(
            "data.coverage", {}, case.as_of, {"freshness_minutes": freshness}, None, None, 0, []
        ),
    )


def _three_cases(conn: duckdb.DuckDBPyConnection, at: datetime) -> list[CaseRecord]:
    """A normal case, a retracted one and one opened while the record was 2,086 minutes behind."""
    normal = new_case(conn, "Sports", "volume_usd", at)
    _coverage(conn, normal, 0.8)
    retracted = new_case(conn, "Politics", "volume_usd", at + timedelta(minutes=5))
    retract_case(conn, retracted.case_id, "judged during the 10-21 outage")
    outage = new_case(conn, "Crypto", "volume_usd", at + timedelta(minutes=10))
    _coverage(conn, outage, 2086.3)
    return [normal, retracted, outage]


def test_digest_alarms_leave_out_retracted_and_outage_cases(market_store: Path) -> None:
    conn = connect(market_store)
    normal, *_ = _three_cases(conn, datetime(2026, 10, 9, 14, 45))
    digest = build_digest(conn, date(2026, 10, 10), datetime(2026, 10, 10, 0, 30))
    conn.close()
    alarms = next(c for c in digest["cards"] if c["id"] == "alarms")
    assert [c["case_id"] for c in alarms["data"]["cases"]] == [normal.case_id]
    assert alarms["sentence"]["text"] == "One investigation opened automatically in the last day."


def test_now_context_leaves_out_retracted_and_outage_cases(market_store: Path) -> None:
    conn = connect(market_store)
    _three_cases(conn, AS_OF - timedelta(hours=3))
    ctx, _ = now_context(conn, AS_OF)
    conn.close()
    assert [c["segment"] for c in ctx["recent_investigations"]] == ["Sports"]


def test_failed_cases_leave_now_list_with_room_for_older_cases(market_store: Path) -> None:
    with connect(market_store) as conn:
        normal = new_case(conn, "Sports", "volume_usd", AS_OF)
        for offset in range(11):
            new_case(
                conn, "Crypto", "volume_usd", AS_OF + timedelta(minutes=offset), state="failed"
            )
        assert [c["case_id"] for c in now_cases(list_cases(conn))] == [normal.case_id]


def test_digest_alarms_leave_out_failed_cases(market_store: Path) -> None:
    with connect(market_store) as conn:
        at = datetime(2026, 10, 9, 14, 45)
        normal = new_case(conn, "Sports", "volume_usd", at)
        new_case(conn, "Crypto", "volume_usd", at, state="failed")
        digest = build_digest(conn, date(2026, 10, 10), datetime(2026, 10, 10, 0, 30))
        alarms = next(c for c in digest["cards"] if c["id"] == "alarms")
        assert [c["case_id"] for c in alarms["data"]["cases"]] == [normal.case_id]
        assert (
            alarms["sentence"]["text"] == "One investigation opened automatically in the last day."
        )


def test_mcp_open_cases_leaves_out_failed_cases(tmp_path: Path) -> None:
    market_store = tmp_path / "cases.duckdb"
    with connect(market_store) as conn:
        normal = new_case(conn, "Sports", "volume_usd", AS_OF)
        new_case(conn, "Crypto", "volume_usd", AS_OF, state="failed")
        new_case(conn, "Politics", "volume_usd", AS_OF, state="closed")
    app = create_app(
        db_path=market_store,
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        clock=lambda: AS_OF,
    )

    tool = app.state.mcp._tool_manager.get_tool("list_open_cases")
    assert tool is not None
    try:
        listed = tool.fn()
        assert [c["case_id"] for c in listed["cases"]] == [normal.case_id]
    finally:
        app.state.conn.close()
