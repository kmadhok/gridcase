"""M7a exit: the semantic layer answers the questions a PM asks next.

New KPIs (markets traded, new markets, whale / sure-thing / buy / long-shot / recurring shares,
trades per trader, a category's share of volume, odds flips, whale wallets, the top market's
share), week and month grains and a month-to-date window, filters with several values, and
limits on trade size and price. Everything still compiles from ontology/kpis.yaml, is validated
and quoted by the compiler, and never sees a trade collected after as_of.

The store: one hour (2026-10-01 10:00) with three markets and hand-countable trades, plus one
earlier trade on 2026-09-30 so a market and a wallet are not new."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from gridcase.planner.llm_schema import tool_inputs
from gridcase.semantic.kpis import (
    FilterValue,
    Grain,
    KpiError,
    KpiQuery,
    Limit,
    compile_kpi,
    load_kpis,
    month_to_date,
    parse_segment,
    run_kpi,
    segment_label,
)
from gridcase.store.db import connect
from gridcase.tools.base import run_tool
from tests.polymarket_synth import add_market, add_trades

H = datetime(2026, 10, 1, 10)
HOUR = (H, H + timedelta(hours=1))
AS_OF = datetime(2026, 10, 1, 12)
EARLIER = datetime(2026, 9, 30, 9)
VOLUME = 500 + 50 + 12_000 + 50  # England 5x$100, Up-or-Down 5x$10, Election $12k + $50


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "m7a.duckdb")
    add_market(conn, "c1", "Sports", "England vs Czechia")
    add_market(conn, "c2", "Crypto", "Bitcoin Up or Down")
    add_market(conn, "c3", "Politics", "Election winner")
    m = timedelta(minutes=1)
    rows: list[dict[str, Any]] = [
        # England: the favourite flips (30c -> 70c over five trades)
        *[
            {"ts": H + (5 + i) * m, "wallet": w, "usd": 100.0, "price": p, "condition_id": "c1"}
            for i, (w, p) in enumerate(zip("abcde", (0.30, 0.35, 0.40, 0.60, 0.70), strict=True))
        ],
        # a 5-minute crypto market flips too, but that is what it is built to do
        *[
            {
                "ts": H + (20 + i) * m,
                "wallet": w,
                "usd": 10.0,
                "price": p,
                "condition_id": "c2",
                "title": "Bitcoin Up or Down - October 1, 6AM ET",
            }
            for i, (w, p) in enumerate(zip("fghij", (0.2, 0.35, 0.5, 0.65, 0.8), strict=True))
        ],
        # one whale on a near-certain outcome, and one small long shot sold
        {"ts": H + 40 * m, "wallet": "W", "usd": 12_000.0, "price": 0.97, "condition_id": "c3"},
        {
            "ts": H + 41 * m,
            "wallet": "a",
            "usd": 50.0,
            "price": 0.05,
            "condition_id": "c3",
            "side": "SELL",
        },
        # the Election market and wallet a traded the day before: not new
        {"ts": EARLIER, "wallet": "a", "usd": 30.0, "condition_id": "c3"},
        # collected after as_of: must never count
        {
            "ts": H + 50 * m,
            "wallet": "late",
            "usd": 9_999.0,
            "condition_id": "c1",
            "collected_at": AS_OF + timedelta(minutes=5),
        },
    ]
    add_trades(conn, rows)
    yield conn
    conn.close()


def total(conn: duckdb.DuckDBPyConnection, kpi: str, **kw: Any) -> float:
    kw.setdefault("window", HOUR)
    rows = run_kpi(conn, KpiQuery(kpi=kpi, grain="total", as_of=AS_OF, **kw))
    return float(rows[0][-1])


@pytest.mark.parametrize(
    ("kpi", "expected"),
    [
        ("volume_usd", VOLUME),
        ("markets_traded", 3),
        ("new_markets", 2),
        ("trades_per_trader", 12 / 11),
        ("whale_share", 12_000 / VOLUME),
        ("sure_thing_share", 12_000 / VOLUME),
        ("buy_share", (VOLUME - 50) / VOLUME),
        ("longshot_share", 1 / 12),
        ("recurring_share", 50 / VOLUME),
        ("odds_flips", 1),
        ("whale_count", 1),
        ("top_market_share", 12_050 / VOLUME),
    ],
)
def test_1_each_new_kpi_matches_a_hand_count(
    store: duckdb.DuckDBPyConnection, kpi: str, expected: float
) -> None:
    assert total(store, kpi) == pytest.approx(expected)


def test_2_a_share_kpi_takes_its_filters_as_the_slice_and_all_volume_as_the_whole(
    store: duckdb.DuckDBPyConnection,
) -> None:
    assert total(store, "volume_share", filters={"category": "Sports"}) == pytest.approx(
        500 / VOLUME
    )
    rows = run_kpi(
        store,
        KpiQuery(kpi="volume_share", grain="total", window=HOUR, as_of=AS_OF, group_by="category"),
    )
    shares = {seg: v for seg, v in rows}
    assert shares["Politics"] == pytest.approx(12_050 / VOLUME)
    assert sum(shares.values()) == pytest.approx(1.0)


def test_3_odds_flips_and_whales_break_down_by_segment(store: duckdb.DuckDBPyConnection) -> None:
    rows = run_kpi(
        store,
        KpiQuery(kpi="odds_flips", grain="total", window=HOUR, as_of=AS_OF, group_by="category"),
    )
    assert {seg: v for seg, v in rows if v} == {"Sports": 1}
    assert total(store, "odds_flips", filters={"category": "Crypto"}) == 0


def test_4_week_month_and_month_to_date(store: duckdb.DuckDBPyConnection) -> None:
    span = (datetime(2026, 9, 28), datetime(2026, 10, 2))

    def by(grain: Grain) -> list[tuple[Any, ...]]:
        return run_kpi(store, KpiQuery(kpi="volume_usd", grain=grain, window=span, as_of=AS_OF))

    hours = sum(v for _, v in by("hour"))
    assert hours == pytest.approx(VOLUME + 30)
    assert by("week") == [(datetime(2026, 9, 28), pytest.approx(VOLUME + 30))]
    assert by("month") == [
        (datetime(2026, 9, 1), pytest.approx(30)),
        (datetime(2026, 10, 1), pytest.approx(VOLUME)),
    ]
    assert month_to_date(AS_OF) == (datetime(2026, 10, 1), AS_OF)
    assert total(store, "volume_usd", window=month_to_date(AS_OF)) == pytest.approx(VOLUME)


def test_5_filters_take_several_values_and_limits(store: duckdb.DuckDBPyConnection) -> None:
    both = total(store, "volume_usd", filters={"category": ["Sports", "Crypto"]})
    assert both == pytest.approx(
        total(store, "volume_usd", filters={"category": "Sports"})
        + total(store, "volume_usd", filters={"category": "Crypto"})
    )
    big = total(store, "volume_usd", limits=[Limit(field="trade_usd", op=">=", value=1000)])
    assert big == pytest.approx(total(store, "volume_usd", filters={"size_band": "$1k+"}))
    cheap = total(store, "trades", limits=[Limit(field="price", op="<", value=0.1)])
    assert cheap == 1


def test_6_bad_filters_and_limits_are_refused(store: duckdb.DuckDBPyConnection) -> None:
    def q(**kw: Any) -> KpiQuery:
        return KpiQuery(kpi="volume_usd", window=HOUR, as_of=AS_OF, **kw)

    with pytest.raises(KpiError, match="no segment"):
        compile_kpi(q(filters={"category": ["Sports", "Chess"]}))
    with pytest.raises(KpiError, match="empty"):
        compile_kpi(q(filters={"category": []}))
    with pytest.raises(KpiError, match="unknown numeric field"):
        compile_kpi(q(limits=[Limit(field="wallet_age", op=">=", value=1)]))
    with pytest.raises(ValueError):
        Limit(field="price", op="!=", value=1)  # type: ignore[arg-type]


def test_7_the_same_question_compiles_to_the_same_sql_and_nothing_leaks(
    store: duckdb.DuckDBPyConnection,
) -> None:
    kpis, _ = load_kpis()
    grains: tuple[Grain, ...] = ("hour", "day", "week", "month", "total")
    for kpi in kpis:
        for grain in grains:
            a = KpiQuery(kpi=kpi, grain=grain, window=HOUR, as_of=AS_OF)
            assert compile_kpi(a).sql == compile_kpi(a).sql
            run_kpi(store, a)
    late = total(store, "volume_usd", window=HOUR)
    assert late == pytest.approx(VOLUME)  # the $9,999 trade collected after as_of is invisible


def test_8_segments_with_several_values_round_trip() -> None:
    f: dict[str, FilterValue] = {"category": ["Sports", "Economy & Finance"], "side": "buy"}
    label = segment_label(f)
    assert label == "category=Economy & Finance|Sports;side=buy"
    assert parse_segment(label) == {"category": ["Economy & Finance", "Sports"], "side": "buy"}


def test_9_tools_and_the_model_can_ask_with_several_values_and_limits(
    store: duckdb.DuckDBPyConnection,
) -> None:
    raw = {
        "kpi": "volume_usd",
        "filters": [
            {"dimension": "category", "value": "Sports"},
            {"dimension": "category", "value": "Politics"},
        ],
        "limits": [{"field": "trade_usd", "op": ">=", "value": 1000}],
        "window": {"start": HOUR[0].isoformat(), "end": HOUR[1].isoformat()},
    }
    inputs = tool_inputs("kpi.compare", raw)
    assert inputs["filters"] == {"category": ["Sports", "Politics"]}
    r = run_tool(store, "kpi.compare", inputs, AS_OF)
    assert r.output["now"] == pytest.approx(12_000)
    by_kind = run_tool(
        store,
        "by.kind",
        {"kpi": "volume_usd", "window": [HOUR[0], HOUR[1]]},
        AS_OF,
    )
    assert {s["segment"] for s in by_kind.output["segments"]} == {"event", "recurring"}
