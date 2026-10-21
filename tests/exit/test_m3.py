"""M3 exit: every watched KPI is checked each hour against the same hour on previous days;
a real jump raises an alarm and opens one case; too little history says "learning"; hours the
collector missed are never judged, including hours after the newest trade while the collector
is down (an empty hour then is unknown, not zero)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from gridcase.market_alarm import WatchedSeries, check_series, load_watch
from gridcase.market_watch import watch_markets
from gridcase.memory.cases import list_cases
from gridcase.planner.llm import FakeLLM
from gridcase.store.db import connect
from tests.polymarket_synth import add_market, add_trades

START = datetime(2026, 10, 1)  # the synthetic record starts here
VOLUME = WatchedSeries(kpi="volume_usd")


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "m3.duckdb")
    add_market(conn, "c1", "Sports", "England vs Czechia")
    add_market(conn, "c2", "Crypto", "Bitcoin up or down")
    yield conn
    conn.close()


def fill(
    db: duckdb.DuckDBPyConnection,
    days: int,
    spike: dict[datetime, float] | None = None,
    since: datetime | None = None,
    collected_at: datetime | None = None,
) -> None:
    """Hourly trading with a daily shape and a little day-to-day wobble; `spike` multiplies
    the dollars of chosen hours. `since` and `collected_at` write only the later hours, as
    collected at one time (a collector catching up after an outage)."""
    spike = spike or {}
    rows = []
    for h in range(days * 24):
        t = START + timedelta(hours=h)
        if since is not None and t < since:
            continue
        base = 1000.0 + 400.0 * (t.hour >= 12)  # afternoons trade more
        wobble = 1.0 + 0.04 * ((h // 24) % 3 - 1)  # -4%, 0, +4% by day
        usd = base * wobble * spike.get(t, 1.0)
        for k in range(10):
            rows.append(
                {
                    "ts": t + timedelta(minutes=5 * k),
                    "wallet": f"w{k}",
                    "usd": usd / 10,
                    "condition_id": "c1" if k < 7 else "c2",
                    "trade_id": f"{h}-{k}",
                    **({"collected_at": collected_at} if collected_at else {}),
                }
            )
    add_trades(db, rows)


def test_1_a_tripled_hour_pair_raises_an_alarm(db: duckdb.DuckDBPyConnection) -> None:
    jump = START + timedelta(days=5, hours=14)
    fill(db, 6, spike={jump: 3.0, jump + timedelta(hours=1): 3.0})
    o = check_series(db, VOLUME, jump + timedelta(hours=2, minutes=20)).output
    assert o["status"] == "alarm" and o["direction"] == "up"
    assert o["run_start"] == jump.isoformat() and o["run_hours"] == 2
    assert 2.5 < o["ratio"] < 3.5  # three times its usual level


def test_2_an_ordinary_day_is_normal(db: duckdb.DuckDBPyConnection) -> None:
    fill(db, 6)
    o = check_series(db, VOLUME, START + timedelta(days=5, hours=16, minutes=20)).output
    assert o["status"] == "normal"


def test_3_two_days_of_history_is_still_learning(db: duckdb.DuckDBPyConnection) -> None:
    jump = START + timedelta(days=2, hours=14)
    fill(db, 3, spike={jump: 3.0, jump + timedelta(hours=1): 3.0})
    o = check_series(db, VOLUME, jump + timedelta(hours=2, minutes=20)).output
    assert o["status"] == "learning" and o["history_days"] < 3


def test_4_an_hour_the_collector_missed_is_not_judged(db: duckdb.DuckDBPyConnection) -> None:
    jump = START + timedelta(days=5, hours=14)
    fill(db, 6, spike={jump: 3.0, jump + timedelta(hours=1): 3.0})
    db.execute(
        "INSERT INTO collector_gaps VALUES (?, ?, ?)",
        [
            jump + timedelta(minutes=10),
            jump + timedelta(hours=1, minutes=20),
            jump + timedelta(hours=2),
        ],
    )
    o = check_series(db, VOLUME, jump + timedelta(hours=2, minutes=20)).output
    assert o["status"] != "alarm" and o["gap_hours_skipped"] == 2


def test_4b_hours_after_the_newest_trade_are_not_judged_until_the_collector_catches_up(
    db: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the collector stops at 13:00 and records no gap: its catch-up has not run yet
    down = START + timedelta(days=5, hours=13)
    fill(db, 6)
    db.execute("DELETE FROM trades WHERE ts >= ?", [down])
    o = check_series(db, VOLUME, down + timedelta(hours=7, minutes=20)).output
    assert o["status"] == "stale" and o["hours_after_data"] == 6 and o["hours_judged"] == 0
    assert o["data_through"] == (down - timedelta(minutes=15)).isoformat()
    # it catches up at 20:30; the hours checked while it was down were not kept as empty
    monkeypatch.setattr("tests.polymarket_synth.clear_cache", lambda: None)
    fill(db, 6, since=down, collected_at=down + timedelta(hours=7, minutes=30))
    o = check_series(db, VOLUME, down + timedelta(hours=8, minutes=20)).output
    assert o["status"] == "normal" and o["hours_after_data"] == 0 and o["hours_judged"] == 6


def test_4c_hours_a_catch_up_has_not_reached_yet_are_not_judged(
    db: duckdb.DuckDBPyConnection,
) -> None:
    # back after a few hours away, the collector walks the feed newest-first: the latest trades
    # are saved, 13:00-17:00 not yet, and its mark still says complete to 13:00
    down = START + timedelta(days=5, hours=13)
    fill(db, 6)
    db.execute("DELETE FROM trades WHERE ts >= ? AND ts < ?", [down, down + timedelta(hours=5)])
    as_of = down + timedelta(hours=7, minutes=20)
    db.execute("INSERT INTO collector_marks VALUES (?, ?)", [down, as_of - timedelta(minutes=5)])
    o = check_series(db, VOLUME, as_of).output
    assert o["status"] == "stale" and o["hours_after_data"] == 6
    assert o["data_through"] == down.isoformat()


def test_5_a_single_extreme_hour_is_enough(db: duckdb.DuckDBPyConnection) -> None:
    jump = START + timedelta(days=5, hours=14)
    fill(db, 6, spike={jump: 20.0})
    o = check_series(db, VOLUME, jump + timedelta(hours=1, minutes=20)).output
    assert o["status"] == "alarm" and o["run_hours"] == 1


def test_6_a_segment_series_is_judged_on_its_own(db: duckdb.DuckDBPyConnection) -> None:
    fill(db, 6)
    o = check_series(
        db,
        WatchedSeries(kpi="volume_usd", filters={"category": "Crypto"}),
        START + timedelta(days=5, hours=16, minutes=20),
    ).output
    assert o["status"] == "normal" and o["series"] == "volume_usd · category=Crypto"


def test_7_the_watch_opens_one_case_per_event_with_its_alarm_as_evidence(
    db: duckdb.DuckDBPyConnection,
) -> None:
    jump = START + timedelta(days=5, hours=14)
    fill(db, 6, spike={jump: 3.0, jump + timedelta(hours=1): 3.0})
    now = jump + timedelta(hours=2, minutes=20)
    first = watch_markets(db, now, FakeLLM, FakeLLM, max_paid_per_day=10, watched=[VOLUME])
    again = watch_markets(
        db, now + timedelta(hours=1), FakeLLM, FakeLLM, max_paid_per_day=10, watched=[VOLUME]
    )
    assert len(first) == 1 and again == []
    case = next(c for c in list_cases(db) if c.case_id == first[0])
    assert case.metric == "volume_usd" and case.entity == "all"
    assert [r.tool for r in case.receipts if r.tool == "kpi.alarm"] == ["kpi.alarm"]


def test_8_the_watched_list_covers_the_team_kpis_and_each_category() -> None:
    watched, params = load_watch()
    kpis = {w.kpi for w in watched if not w.filters}
    assert {
        "volume_usd",
        "active_traders",
        "new_traders",
        "avg_trade_usd",
        "returning_share",
    } <= kpis
    cats = {w.filters.get("category") for w in watched if w.filters}
    assert len(cats) == 8 and params.warmup_days["new_traders"] >= 7
