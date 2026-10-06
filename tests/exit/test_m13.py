"""M13 exit: the Explore page and its semantic-layer chat.

Phase B, the query form and `kpi.series`. The chat model fills in a form, like a Cube query: a
KPI, an optional group-by dimension, filters, a grain and a window. The form's allowed values
come from ontology/kpis.yaml, so a model cannot name a KPI, dimension or segment that does not
exist. Named windows ("yesterday", "last_7d") are resolved by the server at as_of, over complete
hours only. `kpi.series` runs the form through the semantic layer and returns a receipt; it is a
pure function of (inputs, as_of) and never sees a trade collected after as_of.

The store: four markets and hand-countable trades from 2026-10-01 09:30 (the record's first,
partial hour) to 2026-10-05 18:10, asked at 2026-10-05 18:30 UTC."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from gridcase.explore.query import ExploreQuery, QueryError, form_schema, resolve_window
from gridcase.semantic.kpis import is_internal, load_kpis
from gridcase.store.db import connect
from gridcase.tools.base import ToolError, run_tool
from gridcase.tools.registry import ALL_INPUTS, TOOL_NAMES
from tests.polymarket_synth import add_market, add_trades

AS_OF = datetime(2026, 10, 5, 18, 30)
RECORD_START = datetime(2026, 10, 1, 10)  # first complete hour (collection began 09:30)
D = datetime


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "m13.duckdb")
    add_market(conn, "c1", "Sports", "England vs Czechia")
    add_market(conn, "c2", "Sports", "Lakers vs Celtics")
    add_market(conn, "c3", "Politics", "Election winner")
    add_market(conn, "c4", "Crypto", "Bitcoin Up or Down")
    rows: list[dict[str, Any]] = [
        {
            "ts": D(2026, 10, 1, 9, 30),
            "wallet": "z",
            "usd": 5.0,
            "condition_id": "c3",
        },  # before the first complete hour
        {"ts": D(2026, 10, 4, 10, 15), "wallet": "a", "usd": 100.0, "condition_id": "c1"},
        {"ts": D(2026, 10, 4, 10, 20), "wallet": "b", "usd": 300.0, "condition_id": "c1"},
        {"ts": D(2026, 10, 4, 11, 5), "wallet": "c", "usd": 50.0, "condition_id": "c2"},
        {
            "ts": D(2026, 10, 4, 12, 0),
            "wallet": "d",
            "usd": 2000.0,
            "condition_id": "c3",
        },  # a whale
        {"ts": D(2026, 10, 5, 9, 10), "wallet": "a", "usd": 200.0, "condition_id": "c1"},
        {"ts": D(2026, 10, 5, 9, 40), "wallet": "e", "usd": 20.0, "condition_id": "c4"},
        {"ts": D(2026, 10, 5, 17, 50), "wallet": "f", "usd": 80.0, "condition_id": "c2"},
        # traded before as_of but collected after it: invisible at as_of
        {
            "ts": D(2026, 10, 5, 17, 55),
            "wallet": "h",
            "usd": 7.0,
            "condition_id": "c1",
            "collected_at": D(2026, 10, 5, 18, 45),
        },
        # in the hour still running at as_of (18:00-19:00): not a complete hour
        {"ts": D(2026, 10, 5, 18, 10), "wallet": "g", "usd": 1000.0, "condition_id": "c1"},
    ]
    add_trades(conn, rows)
    conn.execute(
        "INSERT INTO collector_gaps VALUES (?, ?, ?)",
        [D(2026, 10, 4, 11, 0), D(2026, 10, 4, 11, 30), D(2026, 10, 4, 11, 40)],
    )
    yield conn
    conn.close()


def q(**kw: Any) -> ExploreQuery:
    return ExploreQuery.model_validate({"kpi": "volume_usd", "window": "yesterday", **kw})


def series(conn: duckdb.DuckDBPyConnection, **kw: Any) -> dict[str, Any]:
    return run_tool(conn, "kpi.series", q(**kw), AS_OF).output


def sports(*extra: str) -> list[dict[str, Any]]:
    return [{"dimension": "category", "values": ["Sports", *extra]}]


# ---------- the form ----------


@pytest.mark.parametrize(
    "bad",
    [
        {"kpi": "nope"},
        {"kpi": "whale_volume"},  # internal measure, not a public KPI
        {"group_by": "outcome"},  # not offered
        {
            "filters": [{"dimension": "market", "values": ["England vs Czechia"]}]
        },  # market: group_by only
        {"filters": [{"dimension": "category", "values": ["Football"]}]},
        {"filters": [{"dimension": "category", "values": []}]},
        {
            "filters": sports() + [{"dimension": "category", "values": ["Crypto"]}]
        },  # one entry per dimension
        {"window": "this_week"},
        {"window": "explicit"},  # needs start and end
        {"window": "explicit", "start": "2026-10-04T12:00", "end": "2026-10-04T11:00"},
        {"window": "yesterday", "start": "2026-10-04T00:00"},  # named windows take no dates
        {"grain": "month"},
        {"top_n": 0},
        {"extra": 1},
    ],
)
def test_the_form_rejects_anything_outside_the_semantic_layer(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        q(**bad)


def test_the_form_accepts_market_as_a_group_by_and_several_segments() -> None:
    form = q(group_by="market", filters=sports("Crypto"), grain="day", window="last_7d", top_n=3)
    assert form.group_by == "market" and form.filters[0].values == ["Sports", "Crypto"]


def test_the_models_schema_offers_exactly_the_public_kpis_and_known_segments() -> None:
    kpis, dims = load_kpis()
    schema = form_schema()
    props = schema["properties"]
    assert set(props["kpi"]["enum"]) == {k for k, v in kpis.items() if not is_internal(v)}
    group_by = next(s for s in props["group_by"]["anyOf"] if s.get("type") == "string")
    assert "market" in group_by["enum"] and "outcome" not in group_by["enum"]
    filt = props["filters"]["items"]["properties"]
    assert "market" not in filt["dimension"]["enum"]
    assert set(filt["values"]["items"]["enum"]) == {
        v for d in filt["dimension"]["enum"] for v in dims[d].values or []
    }
    assert set(props["window"]["enum"]) == {
        "last_24h",
        "last_3d",
        "last_7d",
        "today",
        "yesterday",
        "since_start",
        "explicit",
    }
    assert schema["additionalProperties"] is False


# ---------- windows: complete hours at as_of ----------


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ("last_24h", (D(2026, 10, 4, 18), D(2026, 10, 5, 18))),
        ("last_3d", (D(2026, 10, 2, 18), D(2026, 10, 5, 18))),
        ("last_7d", (D(2026, 9, 28, 18), D(2026, 10, 5, 18))),
        ("today", (D(2026, 10, 5), D(2026, 10, 5, 18))),
        ("yesterday", (D(2026, 10, 4), D(2026, 10, 5))),
        ("since_start", (RECORD_START, D(2026, 10, 5, 18))),
    ],
)
def test_named_windows_resolve_to_complete_utc_hours(
    window: str, expected: tuple[datetime, datetime]
) -> None:
    assert resolve_window(q(window=window), AS_OF, RECORD_START) == expected


def test_explicit_windows_stop_at_the_last_complete_hour() -> None:
    form = q(window="explicit", start="2026-10-05T00:00", end="2026-10-06T00:00")
    assert resolve_window(form, AS_OF, RECORD_START) == (D(2026, 10, 5), D(2026, 10, 5, 18))
    future = q(window="explicit", start="2026-10-06T00:00", end="2026-10-07T00:00")
    with pytest.raises(QueryError, match="no complete hour"):
        resolve_window(future, AS_OF, RECORD_START)


def test_today_just_after_midnight_has_no_complete_hour_yet() -> None:
    with pytest.raises(QueryError, match="no complete hour"):
        resolve_window(q(window="today"), D(2026, 10, 5, 0, 20), RECORD_START)


# ---------- kpi.series: hand counts ----------


def test_a_total_matches_a_hand_count(store: duckdb.DuckDBPyConnection) -> None:
    out = series(store, filters=sports())
    assert out["rows"] == [{"t": None, "segment": None, "value": 450.0}]  # 100 + 300 + 50
    assert out["window"] == {
        "name": "yesterday",
        "start": "2026-10-04T00:00:00",
        "end": "2026-10-05T00:00:00",
    }
    assert out["label"] == "Trading volume" and out["unit"] == "usd"


def test_trades_collected_after_as_of_and_the_running_hour_are_left_out(
    store: duckdb.DuckDBPyConnection,
) -> None:
    # today = 00:00-18:00: 200 + 80 in Sports; the 18:10 trade's hour is still running and the
    # 17:55 trade was collected at 18:45
    assert series(store, filters=sports(), window="today")["rows"][0]["value"] == 280.0


def test_since_start_begins_at_the_first_complete_hour(store: duckdb.DuckDBPyConnection) -> None:
    out = series(store, window="since_start")
    assert (
        out["rows"][0]["value"] == 2750.0
    )  # everything but the 09:30 trade, the 18:10 one and the late one
    assert out["window"]["start"] == "2026-10-01T10:00:00"


def test_a_daily_grain_gives_one_row_per_day(store: duckdb.DuckDBPyConnection) -> None:
    out = series(store, filters=sports(), window="last_3d", grain="day")
    assert out["rows"] == [
        {"t": "2026-10-04T00:00:00", "segment": None, "value": 450.0},
        {"t": "2026-10-05T00:00:00", "segment": None, "value": 280.0},
    ]
    assert out["peak"] == {"t": "2026-10-04T00:00:00", "value": 450.0}
    assert out["low"] == {"t": "2026-10-05T00:00:00", "value": 280.0}


def test_group_by_ranks_segments_and_keeps_the_top_n(store: duckdb.DuckDBPyConnection) -> None:
    out = series(store, group_by="category")
    assert [(r["segment"], r["value"]) for r in out["rows"]] == [
        ("Politics", 2000.0),
        ("Sports", 450.0),
    ]
    top = series(store, group_by="market", filters=sports(), window="last_7d", top_n=1)
    assert [(r["segment"], r["value"]) for r in top["rows"]] == [("England vs Czechia", 600.0)]
    assert top["segments_total"] == 2


def test_several_segments_of_one_dimension_are_added_together(
    store: duckdb.DuckDBPyConnection,
) -> None:
    assert series(store, filters=sports("Crypto"), window="today")["rows"][0]["value"] == 300.0


def test_other_kpis_share_and_count_correctly(store: duckdb.DuckDBPyConnection) -> None:
    assert series(store, kpi="trades", filters=sports())["rows"][0]["value"] == 3.0
    assert series(store, kpi="whale_share")["rows"][0]["value"] == pytest.approx(2000 / 2450)


def test_an_explicit_window_counts_exactly_those_hours(store: duckdb.DuckDBPyConnection) -> None:
    out = series(
        store, filters=sports(), window="explicit", start="2026-10-04T10:00", end="2026-10-04T11:00"
    )
    assert out["rows"][0]["value"] == 400.0


def test_a_window_overlapping_a_collector_gap_says_so(store: duckdb.DuckDBPyConnection) -> None:
    assert series(store)["gap_overlap"] is True  # 2026-10-04 11:00-11:30
    assert series(store, window="today")["gap_overlap"] is False


def test_the_receipt_carries_its_sql_and_inputs(store: duckdb.DuckDBPyConnection) -> None:
    r = run_tool(store, "kpi.series", q(filters=sports()), AS_OF)
    assert r.tool == "kpi.series" and r.sql and "trades_as_of" in r.sql[0]["sql"]
    assert r.inputs["filters"] == [{"dimension": "category", "values": ["Sports"]}]
    again = run_tool(store, "kpi.series", q(filters=sports()), AS_OF)
    assert again.output == r.output  # a pure function of (inputs, as_of)


def test_a_window_with_no_complete_hour_fails_as_a_tool_error(
    store: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(ToolError, match="no complete hour"):
        run_tool(store, "kpi.series", q(window="today"), D(2026, 10, 5, 0, 20))


def test_kpi_series_is_not_offered_to_the_investigation_planner() -> None:
    assert "kpi.series" in ALL_INPUTS and "kpi.series" not in TOOL_NAMES
