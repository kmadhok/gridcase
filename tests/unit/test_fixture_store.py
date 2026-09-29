"""The committed fixture store is what every offline Phase 2-5 test relies on."""

from __future__ import annotations

from datetime import datetime

import duckdb

from tests.conftest import FIXTURE_PARQUET

WINDOWS = [
    (datetime(2021, 2, 13), datetime(2021, 2, 22)),
    (datetime(2023, 5, 1), datetime(2023, 5, 8)),
    (datetime(2023, 7, 15), datetime(2023, 8, 16)),
]


def test_fixture_parquet_is_small_enough_to_commit() -> None:
    assert FIXTURE_PARQUET.stat().st_size < 8 * 1024 * 1024


def test_fixture_store_covers_four_regions_and_all_eia_routes(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    bas = {
        r[0]
        for r in fixture_store.execute(
            "SELECT DISTINCT entity_id FROM observations WHERE series_id = 'eia.D'"
        ).fetchall()
    }
    assert bas == {"ERCO", "CISO", "PJM", "US48"}
    series = {
        r[0]
        for r in fixture_store.execute("SELECT DISTINCT series_id FROM observations").fetchall()
    }
    assert {
        "eia.D",
        "eia.DF",
        "eia.NG",
        "eia.TI",
        "eia.fuel",
        "eia.interchange",
        "eia.subba_demand",
        "om.temp_2m",
        "om.wind_10m",
        "om.cloud_cover",
    } <= series


def test_fixture_store_has_every_hour_of_each_window_for_demand(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    for start, end in WINDOWS:
        n = fixture_store.execute(
            "SELECT count(DISTINCT valid_time) FROM observations WHERE series_id = 'eia.D' "
            "AND entity_id = 'ERCO' AND valid_time >= ? AND valid_time < ?",
            [start, end],
        ).fetchone()
        assert n is not None
        expected = int((end - start).total_seconds() // 3600)
        assert n[0] == expected, f"{start:%Y-%m-%d}: {n[0]} of {expected} hours"


TRAINING_SPANS = [  # ERCO-only history so baselines can be fit before Uri and before May 2023
    (datetime(2020, 2, 1), datetime(2021, 2, 13)),
    (datetime(2022, 5, 1), datetime(2023, 7, 15)),
]


def test_fixture_store_has_erco_training_history(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    for start, end in TRAINING_SPANS:
        for series in ("eia.D", "eia.DF", "eia.NG", "eia.TI", "eia.subba_demand"):
            n = fixture_store.execute(
                "SELECT count(DISTINCT valid_time) FROM observations WHERE series_id = ? "
                "AND entity_id LIKE 'ERCO%' AND valid_time >= ? AND valid_time < ?",
                [series, start, end],
            ).fetchone()
            assert n is not None
            expected = int((end - start).total_seconds() // 3600)
            assert n[0] >= 0.97 * expected, f"{series} {start:%Y-%m}: {n[0]}/{expected}"
        temps = fixture_store.execute(
            "SELECT count(DISTINCT valid_time) FROM observations WHERE series_id = 'om.temp_2m' "
            "AND entity_id = 'wz/houston' AND valid_time >= ? AND valid_time < ?",
            [start, end],
        ).fetchone()
        assert temps is not None and temps[0] >= 0.97 * expected


def test_fixture_store_as_of_macro_works(fixture_store: duckdb.DuckDBPyConnection) -> None:
    n = fixture_store.execute(
        "SELECT count(*) FROM as_of(TIMESTAMP '2021-02-15 12:00:00') WHERE series_id = 'eia.D'"
    ).fetchone()
    assert n is not None and n[0] > 0
