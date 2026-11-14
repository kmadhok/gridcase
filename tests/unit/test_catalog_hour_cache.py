"""Last-hour values are reused only while all their visible inputs are unchanged."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import duckdb
import pytest

import gridcase.catalog as module
from gridcase.semantic.kpis import is_internal, load_kpis, run_kpi
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF, add_market, add_trades
from tests.unit.test_refresh_reads import Noting

HI = AS_OF.replace(minute=0, second=0, microsecond=0)
LO = HI - timedelta(hours=1)


def _bytes(page: dict[str, Any]) -> str:
    return json.dumps(page, separators=(",", ":"))


def _values(page: dict[str, Any]) -> dict[str, Any]:
    return {row["id"]: row["value"] for row in page["kpis"]}


def _fresh(conn: duckdb.DuckDBPyConnection, as_of: datetime = AS_OF) -> dict[str, Any]:
    module.clear_value_cache()
    return module.catalog(conn, as_of)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    module.clear_value_cache()
    conn = connect(tmp_path / "catalog.duckdb")
    add_market(conn, "c1", "Sports", "First event")
    add_market(conn, "c2", "Crypto", "Second event")
    add_trades(
        conn,
        [
            {"ts": LO + timedelta(minutes=1), "wallet": "new", "usd": 100},
            {"ts": LO + timedelta(minutes=2), "wallet": "other", "usd": 300, "condition_id": "c2"},
        ],
    )
    yield conn
    conn.close()
    module.clear_value_cache()


@pytest.fixture()
def queries(monkeypatch: pytest.MonkeyPatch) -> Mock:
    spy = Mock(wraps=run_kpi)
    monkeypatch.setattr(module, "run_kpi", spy)
    # Status is deliberately outside this cache; integration tests use real checks.
    monkeypatch.setattr(
        module, "check_series", Mock(return_value=Mock(output={"status": "normal"}))
    )
    return spy


def test_warm_catalog_is_byte_identical_to_fresh(market_store: Path) -> None:
    with connect(market_store) as conn:
        module.catalog(conn, AS_OF)
        warm = module.catalog(conn, AS_OF)
        assert _bytes(warm) == _bytes(_fresh(conn))


def test_unchanged_inputs_skip_all_value_queries_but_recheck_status(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    module.catalog(db, AS_OF)
    queries.reset_mock()
    module.check_series.reset_mock()  # type: ignore[attr-defined]
    later = module.catalog(db, AS_OF + timedelta(minutes=5))
    queries.assert_not_called()
    assert module.check_series.call_count > 0  # type: ignore[attr-defined]
    assert later["as_of"] == (AS_OF + timedelta(minutes=5)).isoformat()


def test_late_window_trade_invalidates(db: duckdb.DuckDBPyConnection, queries: Mock) -> None:
    before = _values(module.catalog(db, AS_OF))
    add_trades(
        db,
        [
            {
                "ts": LO + timedelta(minutes=10),
                "wallet": "late",
                "usd": 700,
                "collected_at": AS_OF - timedelta(seconds=1),
            }
        ],
    )
    after = module.catalog(db, AS_OF)
    assert _values(after)["volume_usd"] == before["volume_usd"] + 700
    assert _bytes(after) == _bytes(_fresh(db))


def test_older_trade_changes_new_wallet_and_new_market(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    before = _values(module.catalog(db, AS_OF))
    # Earlier DAY: new_traders means first day in our record, not first hour.
    add_trades(
        db,
        [
            {
                "ts": LO - timedelta(days=1),
                "wallet": "new",
                "usd": 50,
                "collected_at": AS_OF - timedelta(seconds=1),
            }
        ],
    )
    after = module.catalog(db, AS_OF)
    assert _values(after)["new_traders"] == before["new_traders"] - 1
    assert _values(after)["new_markets"] == before["new_markets"] - 1
    assert _values(after)["volume_usd"] == before["volume_usd"]
    assert _bytes(after) == _bytes(_fresh(db))


def test_market_category_replacement_invalidates_even_with_same_timestamp(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    before = module.catalog(db, AS_OF)
    queries.reset_mock()
    add_market(db, "c1", "Politics", "First event")
    after = module.catalog(db, AS_OF)
    assert queries.call_count == len(before["kpis"])
    # Unfiltered totals do not use category, but invalidation must cover enrichment.
    assert _bytes(after) == _bytes(before) == _bytes(_fresh(db))


def test_market_event_title_replacement_changes_value(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    before = _values(module.catalog(db, AS_OF))
    add_market(db, "c1", "Sports", "Second event")
    after = module.catalog(db, AS_OF)
    assert _values(after)["top_market_share"] == 1
    assert _values(after)["top_market_share"] != before["top_market_share"]
    assert _bytes(after) == _bytes(_fresh(db))


def test_new_hour_recomputes_and_drops_older_window(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    first = module.catalog(db, AS_OF)
    queries.reset_mock()
    next_hour = module.catalog(db, AS_OF + timedelta(hours=1))
    assert queries.call_count == len(first["kpis"])
    assert next_hour["hour"] != first["hour"]
    queries.reset_mock()
    module.catalog(db, AS_OF)  # the old hour has been discarded
    assert queries.call_count == len(first["kpis"])


def test_different_store_never_reuses_entries(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
    tmp_path: Path,
) -> None:
    before = _values(module.catalog(db, AS_OF))
    with connect(tmp_path / "other.duckdb") as other:
        # Same row count and latest collected_at, different values.
        add_market(other, "c1", "Sports", "First event")
        add_market(other, "c2", "Crypto", "Second event")
        add_trades(
            other,
            [
                {"ts": LO + timedelta(minutes=1), "wallet": "new", "usd": 900},
                {
                    "ts": LO + timedelta(minutes=2),
                    "wallet": "other",
                    "usd": 300,
                    "condition_id": "c2",
                },
            ],
        )
        after = module.catalog(other, AS_OF)
        assert _values(after)["volume_usd"] != before["volume_usd"]
        assert _bytes(after) == _bytes(_fresh(other))


def test_future_collection_does_not_leak_or_invalidate(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    before = module.catalog(db, AS_OF)
    add_trades(
        db,
        [{"ts": LO, "wallet": "future", "usd": 999, "collected_at": AS_OF + timedelta(minutes=1)}],
    )
    queries.reset_mock()
    assert _bytes(module.catalog(db, AS_OF)) == _bytes(before)
    queries.assert_not_called()
    assert _bytes(_fresh(db)) == _bytes(before)
    visible = module.catalog(db, AS_OF + timedelta(minutes=2))
    assert _values(visible)["volume_usd"] == _values(before)["volume_usd"] + 999


def test_trades_at_or_after_hi_do_not_invalidate(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    before = module.catalog(db, AS_OF)
    add_trades(db, [{"ts": HI, "wallet": "new", "usd": 999, "collected_at": AS_OF}])
    queries.reset_mock()
    assert _bytes(module.catalog(db, AS_OF)) == _bytes(before)
    queries.assert_not_called()
    assert _bytes(_fresh(db)) == _bytes(before)


def test_fingerprint_is_one_query_per_catalog(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    module.catalog(db, AS_OF)
    seen: list[str] = []
    module.catalog(Noting(db, seen), AS_OF)  # type: ignore[arg-type]
    # One store-identity lookup, one input fingerprint, no KPI or status SQL.
    assert len(seen) == 2
    assert sum("trades_as_of" in sql for sql in seen) == 1


def test_empty_total_values_are_reused(tmp_path: Path, queries: Mock) -> None:
    with connect(tmp_path / "empty.duckdb") as conn:
        first = module.catalog(conn, AS_OF)
        queries.reset_mock()
        assert _bytes(module.catalog(conn, AS_OF)) == _bytes(first)
        queries.assert_not_called()
        assert set(_values(first)) == {k for k, v in load_kpis()[0].items() if not is_internal(v)}
        assert _bytes(first) == _bytes(_fresh(conn))


def test_fingerprint_uses_same_second_precision_as_kpi_sql(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    add_trades(
        db,
        [
            {
                "ts": LO,
                "wallet": "fraction",
                "usd": 777,
                "collected_at": AS_OF + timedelta(microseconds=1),
            }
        ],
    )
    first = module.catalog(db, AS_OF)
    queries.reset_mock()
    after = module.catalog(db, AS_OF + timedelta(microseconds=999999))
    queries.assert_not_called()
    assert _values(first) == _values(after)
    assert _bytes(after) == _bytes(_fresh(db, AS_OF + timedelta(microseconds=999999)))


def test_separate_memory_stores_do_not_share_values(queries: Mock) -> None:
    with connect(":memory:") as first, connect(":memory:") as second:
        for conn, usd in [(first, 100), (second, 900)]:
            add_trades(conn, [{"ts": LO, "wallet": "same", "usd": usd}])
        assert _values(module.catalog(first, AS_OF))["volume_usd"] == 100
        assert _values(module.catalog(second, AS_OF))["volume_usd"] == 900


def test_backdated_collection_changes_count_without_advancing_max(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    before = _values(module.catalog(db, AS_OF))
    add_trades(db, [{"ts": LO, "wallet": "backdated", "usd": 500, "collected_at": LO}])
    after = module.catalog(db, AS_OF)
    assert _values(after)["volume_usd"] == before["volume_usd"] + 500
    assert _bytes(after) == _bytes(_fresh(db))


def test_earlier_as_of_with_same_count_cannot_reuse_later_knowledge(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    add_trades(
        db, [{"ts": LO, "wallet": "late", "usd": 700, "collected_at": AS_OF - timedelta(seconds=1)}]
    )
    before = _values(module.catalog(db, AS_OF))
    # At the earlier cutoff, one row disappears and one backdated row appears: same count.
    add_trades(db, [{"ts": LO, "wallet": "backdated", "usd": 900, "collected_at": LO}])
    earlier = AS_OF - timedelta(minutes=1)
    after = module.catalog(db, earlier)
    assert _values(after)["volume_usd"] == before["volume_usd"] + 200
    assert _bytes(after) == _bytes(_fresh(db, earlier))


def test_new_cursor_on_same_file_reuses_values(
    db: duckdb.DuckDBPyConnection,
    queries: Mock,
) -> None:
    first = module.catalog(db, AS_OF)
    queries.reset_mock()
    with db.cursor() as cursor:
        assert _bytes(module.catalog(cursor, AS_OF)) == _bytes(first)
    queries.assert_not_called()


# ------------------------------------------------------------------ review round 1 (2026-11-14)


def test_swapping_event_titles_between_markets_invalidates(
    db: duckdb.DuckDBPyConnection, queries: Mock
) -> None:
    """DuckDB's hash(a, b, c) XORed over rows cancels when one column's values swap between
    rows. Here the swap regroups volume by event: top_market_share goes from 0.5 to 0.83."""
    add_market(db, "c3", "Sports", "First event")
    add_trades(
        db, [{"ts": LO + timedelta(minutes=3), "wallet": "w3", "usd": 200, "condition_id": "c3"}]
    )
    before = _values(module.catalog(db, AS_OF))["top_market_share"]
    add_market(db, "c1", "Sports", "Second event")
    add_market(db, "c2", "Crypto", "First event")
    after = module.catalog(db, AS_OF)
    assert _values(after)["top_market_share"] != before
    assert _bytes(after) == _bytes(_fresh(db))


def test_trade_loaded_by_another_cursor_mid_catalog_is_not_cached_under_old_inputs(
    db: duckdb.DuckDBPyConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        module, "check_series", Mock(return_value=Mock(output={"status": "normal"}))
    )
    writer = db.cursor()
    fired = []

    def racing_run_kpi(conn: duckdb.DuckDBPyConnection, q: Any) -> Any:
        if not fired:
            fired.append(1)
            add_trades(
                writer,
                [
                    {
                        "ts": LO + timedelta(minutes=10),
                        "wallet": "late",
                        "usd": 700,
                        "collected_at": AS_OF - timedelta(seconds=1),
                    }
                ],
            )
        return run_kpi(conn, q)

    monkeypatch.setattr(module, "run_kpi", racing_run_kpi)
    module.catalog(db, AS_OF)  # the trade lands while this computes
    replay = AS_OF - timedelta(seconds=30)  # same hour window, before the trade was collected
    got_replay = module.catalog(db, replay)
    got_now = module.catalog(db, AS_OF)
    monkeypatch.setattr(module, "run_kpi", run_kpi)
    assert _bytes(got_replay) == _bytes(_fresh(db, replay))
    assert _bytes(got_now) == _bytes(_fresh(db, AS_OF))
    writer.close()


def test_attached_memory_databases_do_not_share_values(queries: Mock) -> None:
    conn = connect(":memory:")
    conn.execute("ATTACH ':memory:' AS two")
    conn.execute("USE two")
    from gridcase.store.schema import create_schema

    create_schema(conn)
    for name, usd in (("memory", 100), ("two", 900)):
        conn.execute(f"USE {name}")
        add_market(conn, "c1", "Sports", "First event")
        add_trades(conn, [{"ts": LO + timedelta(minutes=1), "wallet": "w", "usd": usd}])
    conn.execute("USE memory")
    one = _values(module.catalog(conn, AS_OF))["volume_usd"]
    conn.execute("USE two")
    two = _values(module.catalog(conn, AS_OF))["volume_usd"]
    assert (one, two) == (100, 900)
    conn.close()


def test_inside_a_callers_transaction_catalog_still_works(
    db: duckdb.DuckDBPyConnection, queries: Mock
) -> None:
    db.execute("BEGIN TRANSACTION")
    try:
        assert _bytes(module.catalog(db, AS_OF)) == _bytes(_fresh(db))
    finally:
        db.execute("ROLLBACK")
