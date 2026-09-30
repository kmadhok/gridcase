"""M7b exit: the new KPIs are watched every hour and investigated like the old ones.

Eighteen series are watched (five new: markets traded, whale share, sure-thing share, odds
flips, the top market's share). On the synthetic 9-day market: a burst of $5k trades spread
over every market alarms the whale share and is explained by big trades; odds flipping in one
match alarm the flip count and are explained by that market. Every measured sentence cites
evidence. Settled hours are reused from the cache, and the hour still settling is not."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from gridcase.explain.verifier import verify
from gridcase.market_alarm import WatchedSeries, check_series, load_watch
from gridcase.market_watch import watch_markets
from gridcase.memory.cases import list_cases
from gridcase.planner.llm import FakeLLM
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF, MARKETS, add_trades, window_hours

NEW = {"markets_traded", "whale_share", "sure_thing_share", "odds_flips", "top_market_share"}


@pytest.fixture()
def db(market_store: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(market_store)
    yield conn
    conn.close()


def _investigated(db: duckdb.DuckDBPyConnection, kpi: str) -> Any:
    opened = watch_markets(
        db, AS_OF, FakeLLM, FakeLLM, max_paid_per_day=10, watched=[WatchedSeries(kpi=kpi)]
    )
    assert len(opened) == 1, f"{kpi} did not alarm"
    case = next(c for c in list_cases(db) if c.case_id == opened[0])
    assert case.conclusion is not None and case.plan is not None
    receipts = {r.receipt_id: r for r in case.receipts}
    for claim in case.conclusion.claims:
        if claim.tier != "inferred":
            assert claim.receipt_ids and set(claim.receipt_ids) <= set(receipts), claim.text
    _, dropped = verify(case.conclusion, receipts)
    assert dropped == []
    return case


def test_1_eighteen_series_are_watched_and_all_report_a_status(
    db: duckdb.DuckDBPyConnection,
) -> None:
    watched, params = load_watch()
    assert len(watched) == 18
    assert {w.kpi for w in watched if not w.filters} >= NEW
    for w in watched:
        assert check_series(db, w, AS_OF, params).output["status"] in {"normal", "learning"}


def test_2_a_burst_of_big_trades_alarms_the_whale_share_and_names_big_trades(
    db: duckdb.DuckDBPyConnection,
) -> None:
    add_trades(
        db,
        [
            {
                "ts": h + timedelta(minutes=45 + i),
                "wallet": f"whale{i}",
                "usd": 5_000.0,
                "condition_id": cid,
                "trade_id": f"wh{h}-{cid}",
            }
            for h in window_hours()
            for i, cid in enumerate(MARKETS)
        ],
    )
    case = _investigated(db, "whale_share")
    assert case.top_hypothesis == "D4_WHALES"


def test_3_odds_flipping_in_one_match_alarms_and_names_the_match(
    db: duckdb.DuckDBPyConnection,
) -> None:
    add_trades(
        db,
        [
            {
                "ts": h + timedelta(minutes=10 + 5 * k),
                "wallet": f"w{k}",
                "usd": 50.0,
                "price": p,
                "condition_id": "m1",
                "trade_id": f"flip{h}-{k}",
            }
            for h in window_hours()
            for k, p in enumerate((0.3, 0.35, 0.4, 0.6, 0.7))
        ],
    )
    case = _investigated(db, "odds_flips")
    assert case.top_hypothesis == "D1_ONE_MARKET"
    assert any("England vs Czechia" in c.text for c in case.conclusion.claims)


def test_4_settled_hours_come_from_the_cache_and_the_settling_hour_does_not(
    db: duckdb.DuckDBPyConnection,
) -> None:
    series = WatchedSeries(kpi="volume_usd")
    first = check_series(db, series, AS_OF).output
    assert check_series(db, series, AS_OF).output == first
    # a trade in the hour that is still settling (13:00-14:00 at 14:20) is seen on the next run
    last_hour = AS_OF.replace(minute=0) - timedelta(hours=1)
    db.execute(
        "INSERT INTO trades SELECT 'late-1', ?, 'w1', 'BUY', 20000, 0.5, 10000, 'm1-yes', 'm1', "
        "'slug', 'ev', 'Yes', 'Will it happen?', 'tx', ?",
        [last_hour + timedelta(minutes=5), last_hour + timedelta(minutes=7)],
    )
    after = check_series(db, series, AS_OF).output
    assert after["status"] == "alarm" and after["direction"] == "up"
    # an hour that settled long ago is reused as computed: live collection never adds to it
    old_hour = AS_OF.replace(minute=0) - timedelta(hours=5)
    db.execute(
        "INSERT INTO trades SELECT 'back-1', ?, 'w1', 'BUY', 2e6, 0.5, 1e6, 'm1-yes', 'm1', "
        "'slug', 'ev', 'Yes', 'Will it happen?', 'tx', ?",
        [old_hour + timedelta(minutes=5), old_hour + timedelta(minutes=7)],
    )
    assert check_series(db, series, AS_OF).output == after


@pytest.mark.parametrize("kpi", sorted(NEW))
def test_5_every_new_kpi_can_be_investigated(db: duckdb.DuckDBPyConnection, kpi: str) -> None:
    from gridcase.investigate import investigate

    case = investigate(db, FakeLLM(), kpi, AS_OF)
    assert case.conclusion is not None and case.top_hypothesis
    receipts = {r.receipt_id: r for r in case.receipts}
    _, dropped = verify(case.conclusion, receipts)
    assert dropped == [] and not case.step_errors
