"""M2 exit: the KPIs a product team owns are defined once and compiled to SQL; they can be
filtered and broken down by any dimension; nothing the collector saw after as_of leaks in."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from gridcase.ingest.polymarket_load import load_raw
from gridcase.semantic.kpis import KpiError, KpiQuery, compile_kpi, load_kpis, run_kpi
from gridcase.store.db import connect
from tests.polymarket_synth import add_market, add_trades

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "polymarket"
HOUR = (datetime(2026, 9, 29, 19), datetime(2026, 9, 29, 20))
AS_OF = datetime(2026, 9, 29, 21)
DAY = datetime(2026, 9, 29)


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> Iterator[duckdb.DuckDBPyConnection]:
    root = tmp_path_factory.mktemp("m2")
    shutil.copytree(FIXTURE, root / "raw")
    conn = connect(root / "m2.duckdb")
    load_raw(conn, root / "raw")
    cats = ["Sports", "Crypto", "Politics", "Weather"]
    for i, (cid,) in enumerate(
        conn.execute("SELECT DISTINCT condition_id FROM trades ORDER BY 1").fetchall()
    ):
        add_market(conn, cid, cats[i % 4], event_title=f"Event {i % 40}")
    yield conn
    conn.close()


@pytest.fixture()
def synth(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "s.duckdb")
    add_market(conn, "c1", "Sports", "England vs Czechia")
    add_market(conn, "c2", "Crypto", "Bitcoin up or down")
    yield conn
    conn.close()


def test_1_every_kpi_runs_by_hour_and_day_with_every_breakdown(
    real: duckdb.DuckDBPyConnection,
) -> None:
    kpis, dims = load_kpis()
    assert {
        "volume_usd",
        "trades",
        "active_traders",
        "new_traders",
        "avg_trade_usd",
        "returning_share",
    } <= set(kpis)
    assert {"category", "market", "size_band", "trader_type", "side", "odds_band"} <= set(dims)
    for kpi in kpis:
        for grain in ("hour", "day"):
            for group_by in [None, *dims]:
                rows = run_kpi(
                    real,
                    KpiQuery(kpi=kpi, grain=grain, window=HOUR, as_of=AS_OF, group_by=group_by),
                )
                assert rows, (kpi, grain, group_by)
                assert all(len(r) == (3 if group_by else 2) for r in rows)


def test_2_additive_kpis_break_down_into_parts_that_sum_to_the_whole(
    real: duckdb.DuckDBPyConnection,
) -> None:
    _, dims = load_kpis()
    for kpi in ("volume_usd", "trades"):
        total = run_kpi(real, KpiQuery(kpi=kpi, grain="total", window=HOUR, as_of=AS_OF))[0][-1]
        for dim in dims:
            parts = run_kpi(
                real, KpiQuery(kpi=kpi, grain="total", window=HOUR, as_of=AS_OF, group_by=dim)
            )
            assert abs(sum(r[-1] for r in parts) - total) <= 1e-6 * max(1.0, total), (kpi, dim)


def test_3_filters_select_a_segment(real: duckdb.DuckDBPyConnection) -> None:
    whole = run_kpi(real, KpiQuery(kpi="volume_usd", grain="total", window=HOUR, as_of=AS_OF))[0][
        -1
    ]
    sports = run_kpi(
        real,
        KpiQuery(
            kpi="volume_usd",
            grain="total",
            window=HOUR,
            as_of=AS_OF,
            filters={"category": "Sports"},
        ),
    )[0][-1]
    by_cat = dict(
        (r[0], r[1])
        for r in run_kpi(
            real,
            KpiQuery(
                kpi="volume_usd", grain="total", window=HOUR, as_of=AS_OF, group_by="category"
            ),
        )
    )
    assert 0 < sports < whole and abs(sports - by_cat["Sports"]) < 1e-6
    with pytest.raises(KpiError):
        compile_kpi(
            KpiQuery(
                kpi="volume_usd",
                grain="total",
                window=HOUR,
                as_of=AS_OF,
                filters={"planet": "Mars"},
            )
        )
    with pytest.raises(KpiError):
        compile_kpi(
            KpiQuery(
                kpi="volume_usd",
                grain="total",
                window=HOUR,
                as_of=AS_OF,
                filters={"size_band": "huge"},
            )
        )
    # a market name with a quote in it is a value, never SQL
    q = KpiQuery(
        kpi="trades",
        grain="total",
        window=HOUR,
        as_of=AS_OF,
        filters={"market": "Will O'Brien win?'; DROP TABLE trades; --"},
    )
    assert run_kpi(real, q) in ([], [(0,)], [(None,)]) or run_kpi(real, q)[0][-1] in (0, None)
    assert real.execute("SELECT count(*) FROM trades").fetchone()[0] > 0  # type: ignore[index]


def test_4_the_same_question_gives_the_same_sql_and_answer(real: duckdb.DuckDBPyConnection) -> None:
    q = KpiQuery(kpi="avg_trade_usd", grain="hour", window=HOUR, as_of=AS_OF, group_by="category")
    assert compile_kpi(q).sql == compile_kpi(q).sql
    assert run_kpi(real, q) == run_kpi(real, q)
    assert "TIMESTAMP '2026-09-29 21:00:00'" in compile_kpi(q).sql  # self-contained, replayable


def test_5_derived_kpis_match_a_hand_count(synth: duckdb.DuckDBPyConnection) -> None:
    t = DAY + timedelta(hours=10)
    add_trades(
        synth,
        [
            {
                "ts": t - timedelta(days=1),
                "wallet": "old",
                "usd": 5,
            },  # old trader, first seen yesterday
            {"ts": t, "wallet": "old", "usd": 30, "condition_id": "c1"},
            {"ts": t + timedelta(minutes=5), "wallet": "new", "usd": 10, "condition_id": "c1"},
            {"ts": t + timedelta(minutes=9), "wallet": "new", "usd": 20, "condition_id": "c2"},
        ],
    )
    w = (t, t + timedelta(hours=1))
    as_of = t + timedelta(hours=2)

    def total(kpi: str) -> float:
        rows = run_kpi(synth, KpiQuery(kpi=kpi, grain="total", window=w, as_of=as_of))
        return float(rows[0][-1])

    assert total("volume_usd") == 60 and total("trades") == 3
    assert total("avg_trade_usd") == 20
    assert total("active_traders") == 2 and total("new_traders") == 1
    assert total("returning_share") == 0.5
    by_trader = dict(
        (r[0], r[1])
        for r in run_kpi(
            synth,
            KpiQuery(
                kpi="volume_usd", grain="total", window=w, as_of=as_of, group_by="trader_type"
            ),
        )
    )
    assert by_trader == {"new": 30.0, "returning": 30.0}
    by_market = dict(
        (r[0], r[1])
        for r in run_kpi(
            synth,
            KpiQuery(kpi="volume_usd", grain="total", window=w, as_of=as_of, group_by="market"),
        )
    )
    assert by_market == {"England vs Czechia": 40.0, "Bitcoin up or down": 20.0}


def test_6_trades_collected_after_as_of_are_invisible(synth: duckdb.DuckDBPyConnection) -> None:
    t = DAY + timedelta(hours=10)
    add_trades(
        synth,
        [
            {"ts": t, "wallet": "a", "usd": 10, "collected_at": t + timedelta(minutes=2)},
            {
                "ts": t + timedelta(minutes=1),
                "wallet": "b",
                "usd": 99,
                "collected_at": t + timedelta(hours=3),
            },
        ],
    )
    w = (t, t + timedelta(hours=1))
    early = run_kpi(
        synth, KpiQuery(kpi="volume_usd", grain="total", window=w, as_of=t + timedelta(hours=1))
    )
    late = run_kpi(
        synth, KpiQuery(kpi="volume_usd", grain="total", window=w, as_of=t + timedelta(hours=4))
    )
    assert early[0][-1] == 10 and late[0][-1] == 109
