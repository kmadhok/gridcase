"""M12 exit: trader retention by first-day cohort, in the dbt warehouse.

A cohort is the wallets whose first trade in our record falls on a UTC day. Day N retention is
the share of the cohort that trades again N days later. The record's first (partial) day is not
a cohort (every wallet looks new on it) and the latest day is incomplete, so neither appears.
Cohorts in the record's first week are flagged as not warmed up: some of their "new" wallets
traded before collection began. Planted wallets on a synthetic record give exact answers."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from gridcase.config import ROOT
from gridcase.ingest.export import export_markets
from gridcase.ingest.polymarket_load import load_raw
from gridcase.store.db import connect
from tests.polymarket_synth import add_market

DBT_DIR = ROOT / "dbt"
D = date(2026, 9, 29)  # the record's first day
MARKET = "0xmarket"

# wallet -> the days (offsets from D) it trades on
ACTIVITY = {
    "w0": [0, 1],  # first seen on the record's first day: not a cohort, not new on day 1
    "w1": [1, 2, 3],
    "w2": [1, 3],
    "w3": [1],
    "w4": [1, 2],
    "w5": [2, 3],
    "w6": [2],
    "w7": [7, 8],  # a cohort a full week after the record began: warmed up
    "w8": [9],  # trades only on the latest, incomplete day
}


def _trade(wallet: str, day: int, n: int) -> dict[str, Any]:
    ts = datetime(D.year, D.month, D.day, 20 if day == 0 else 12) + timedelta(days=day)
    return {
        "proxyWallet": wallet,
        "side": "BUY",
        "asset": "a1",
        "conditionId": MARKET,
        "size": 10.0 + n,
        "price": 0.5,
        "timestamp": int(ts.timestamp()),
        "title": "Will it happen?",
        "slug": "will-it-happen",
        "eventSlug": "an-event",
        "outcome": "Yes",
        "transactionHash": f"0x{wallet}{day}{n}",
        "collected_at": (ts + timedelta(minutes=5)).isoformat() + "Z",
    }


def dbt(*args: str) -> None:
    from dbt.cli.main import dbtRunner

    res = dbtRunner().invoke(
        [*args, "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR), "--quiet"]
    )
    results: Any = res.result or []
    failed = [(r.node.name, r.status, r.message) for r in results if r.status != "pass"]
    assert res.success, res.exception or failed


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    tmp = tmp_path_factory.mktemp("m12")
    raw = tmp / "raw"
    by_day: dict[int, list[dict[str, Any]]] = {}
    for n, (wallet, days) in enumerate(ACTIVITY.items()):
        for day in days:
            by_day.setdefault(day, []).append(_trade(wallet, day, n))
    for day, trades in by_day.items():
        d = D + timedelta(days=day)
        hour = 20 if day == 0 else 12
        f = raw / "trades" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / f"{hour:02d}.jsonl.gz"
        f.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(f, "wt") as out:
            out.writelines(json.dumps(t) + "\n" for t in trades)
    app = connect(tmp / "app.duckdb")
    load_raw(app, raw)
    add_market(app, MARKET, "Politics")
    export_markets(app, tmp / "exports" / "markets.parquet")
    app.close()
    mp = pytest.MonkeyPatch()
    mp.setenv("MARKETCASE_RAW", str(raw))
    mp.setenv("MARKETCASE_EXPORTS", str(tmp / "exports"))
    mp.setenv("MARKETCASE_WAREHOUSE", str(tmp / "warehouse.duckdb"))
    dbt("build")  # every dbt test, including the retention tests, must pass
    yield tmp / "warehouse.duckdb"
    mp.undo()


def rows(warehouse: Path) -> dict[tuple[int, int], tuple[int, int, float, bool]]:
    w = duckdb.connect(str(warehouse))  # same settings as dbt's open connection
    out = w.execute(
        "SELECT cohort_day, day_number, activity_day, cohort_size, retained_traders, "
        "retention_rate, warmed_up FROM main_marts.trader_retention"
    ).fetchall()
    w.close()
    got = {}
    for cohort, n, activity, size, kept, rate, warm in out:
        assert activity == cohort + timedelta(days=n)
        got[((cohort - D).days, n)] = (size, kept, rate, warm)
    return got


def test_1_cohorts_and_retention_match_the_planted_wallets(warehouse: Path) -> None:
    # Latest day (D+9) is incomplete, so the last complete day is D+8.
    want = {
        (1, 0): (4, 4, 1.0),
        (1, 1): (4, 2, 0.5),  # w1, w4
        (1, 2): (4, 2, 0.5),  # w1, w2
        (2, 0): (2, 2, 1.0),
        (2, 1): (2, 1, 0.5),  # w5
        (7, 0): (1, 1, 1.0),
        (7, 1): (1, 1, 1.0),  # w7
    }
    for n in range(3, 8):  # cohort D+1 is followed up to D+8; nobody came back after D+3
        want[(1, n)] = (4, 0, 0.0)
    for n in range(2, 7):  # cohort D+2 up to D+8
        want[(2, n)] = (2, 0, 0.0)
    got = rows(warehouse)
    assert {k: v[:3] for k, v in got.items()} == want


def test_2_the_first_and_latest_days_are_not_cohorts(warehouse: Path) -> None:
    cohorts = {c for c, _ in rows(warehouse)}
    assert 0 not in cohorts  # w0 was already trading when collection began
    assert 9 not in cohorts and max(c + n for c, n in rows(warehouse)) == 8


def test_3_only_cohorts_a_week_into_the_record_are_warmed_up(warehouse: Path) -> None:
    warm = {c: v[3] for (c, _), v in rows(warehouse).items()}
    assert warm == {1: False, 2: False, 7: True}


def test_4_cohort_size_equals_the_daily_marts_new_traders(warehouse: Path) -> None:
    w = duckdb.connect(str(warehouse))  # same settings as dbt's open connection
    pairs = w.execute(
        "SELECT r.cohort_size, m.new_traders FROM main_marts.trader_retention r "
        "JOIN main_marts.metrics_daily m ON m.day = r.cohort_day WHERE r.day_number = 0"
    ).fetchall()
    w.close()
    assert len(pairs) == 3 and all(a == b for a, b in pairs)
