"""Shared fixtures. `fixture_store` is a real DuckDB built from committed parquet snapshots."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from gridcase.store.db import connect

FIXTURE_PARQUET = Path(__file__).resolve().parent / "fixtures" / "store" / "observations.parquet"


@pytest.fixture(scope="session")
def fixture_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[duckdb.DuckDBPyConnection]:
    """Store with three windows for ERCO/CISO/PJM/US48: Uri 2021-02-13..22, a normal week
    2023-05-01..08, and summer 2023-07-15..08-16, all EIA routes plus Open-Meteo.
    Rebuild the parquet with scripts/make_fixture_store.py."""
    if not FIXTURE_PARQUET.exists():
        pytest.fail(f"missing {FIXTURE_PARQUET}; run scripts/make_fixture_store.py")
    conn = connect(tmp_path_factory.mktemp("store") / "fixture.duckdb")
    conn.execute("INSERT INTO observations SELECT * FROM read_parquet(?)", [str(FIXTURE_PARQUET)])
    yield conn
    conn.close()
