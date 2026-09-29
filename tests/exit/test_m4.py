"""M4 exit: an investigation finds the segment that moved a KPI, like an analyst would.

A synthetic 9-day market (six markets in three categories, returning traders, ordinary trade
sizes at even odds) gets one event injected into the last six hours; the investigation's top
driver must name it, and every measured sentence must cite evidence that checks out."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from gridcase.investigate import investigate
from gridcase.planner.llm import FakeLLM
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF, MARKETS, WINDOW_START, add_trades, window_hours


@pytest.fixture()
def db(market_store: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(market_store)
    yield conn
    conn.close()


def _window_hours() -> list[datetime]:
    return window_hours()


def _run(db: duckdb.DuckDBPyConnection) -> Any:
    case = investigate(db, FakeLLM(), "volume_usd", AS_OF)
    assert case.plan is not None and len(case.plan.steps) <= 12
    assert case.conclusion is not None
    receipts = {r.receipt_id for r in case.receipts}
    for c in case.conclusion.claims:
        if c.tier != "inferred":
            assert c.receipt_ids and set(c.receipt_ids) <= receipts, c.text
    return case


def test_1_one_market_tripled_is_named(db: duckdb.DuckDBPyConnection) -> None:
    extra = [
        {
            "ts": h + timedelta(minutes=30 + k),
            "wallet": f"w{k}",
            "usd": 300.0 * 2,
            "condition_id": "m1",
            "trade_id": f"x{h}-{k}",
        }
        for h in _window_hours()
        for k in range(4)
    ]
    add_trades(db, extra)
    case = _run(db)
    assert case.top_hypothesis == "D1_ONE_MARKET"
    status = {v.id: v.status for v in case.conclusion.ranked}
    assert status["D6_BROAD_BASED"] == "refuted"
    assert any("England vs Czechia" in c.text for c in case.conclusion.claims)


def test_2_everything_up_evenly_is_broad_based(db: duckdb.DuckDBPyConnection) -> None:
    extra = [
        {
            "ts": h + timedelta(minutes=40 + i),
            "wallet": f"w{(i + k) % 40}",
            "usd": 60.0 if k % 2 else 240.0,
            "condition_id": cid,
            "trade_id": f"y{h}-{cid}-{k}",
        }
        for h in _window_hours()
        for i, cid in enumerate(MARKETS)
        for k in range(4)
    ]
    add_trades(db, extra)
    case = _run(db)
    assert case.top_hypothesis == "D6_BROAD_BASED"


def test_3_a_wave_of_new_wallets_is_named(db: duckdb.DuckDBPyConnection) -> None:
    extra = [
        {
            "ts": h + timedelta(minutes=50 + i),
            "wallet": f"new{h.hour}-{i}-{k}",
            "usd": 150.0,
            "condition_id": cid,
            "trade_id": f"z{h}-{cid}-{k}",
        }
        for h in _window_hours()
        for i, cid in enumerate(MARKETS)
        for k in range(4)
    ]
    add_trades(db, extra)
    case = _run(db)
    assert case.top_hypothesis == "D3_NEW_TRADERS"


def test_4_a_few_whale_trades_are_named(db: duckdb.DuckDBPyConnection) -> None:
    extra = [
        {
            "ts": WINDOW_START + timedelta(hours=j, minutes=45),
            "wallet": f"w{j}",
            "usd": 9000.0,
            "condition_id": cid,
            "trade_id": f"whale{j}",
        }
        for j, cid in enumerate(["m1", "m3", "m5", "m2", "m4", "m6"])
    ]
    add_trades(db, extra)
    case = _run(db)
    assert case.top_hypothesis == "D4_WHALES"


def test_5_a_collector_gap_is_a_data_problem_first(db: duckdb.DuckDBPyConnection) -> None:
    gap_from = WINDOW_START + timedelta(hours=2, minutes=5)
    db.execute(
        "INSERT INTO collector_gaps VALUES (?, ?, ?)",
        [gap_from, gap_from + timedelta(minutes=20), gap_from + timedelta(minutes=25)],
    )
    case = _run(db)
    assert case.top_hypothesis == "D0_DATA_PROBLEM"


def test_6_the_first_step_is_always_the_data_check(db: duckdb.DuckDBPyConnection) -> None:
    case = _run(db)
    assert case.plan.hypotheses[0] == "D0_DATA_PROBLEM"
    assert case.plan.steps[0].tool == "data.coverage"


def test_7_an_ordinary_window_says_there_is_nothing_unusual(db: duckdb.DuckDBPyConnection) -> None:
    """Asked about hours that look like any other day, it says so instead of inventing a cause."""
    case = _run(db)
    assert any("usual range" in c for c in case.conclusion.caveats)


def test_8_an_unusual_window_carries_no_such_note(db: duckdb.DuckDBPyConnection) -> None:
    extra = [
        {
            "ts": h + timedelta(minutes=30 + k),
            "wallet": f"w{k}",
            "usd": 600.0,
            "condition_id": "m1",
            "trade_id": f"x{h}-{k}",
        }
        for h in _window_hours()
        for k in range(4)
    ]
    add_trades(db, extra)
    case = _run(db)
    assert not any("usual range" in c for c in case.conclusion.caveats)
