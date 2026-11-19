"""The store caps DuckDB's spill files: the default (90% of free space as Linux sees it) let a cold
start write ~22 GB to `<store>.tmp` on kayak, where the real free space is the Windows C: drive."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from gridcase.store.db import connect


def _temp_cap(conn: duckdb.DuckDBPyConnection) -> str:
    row = conn.execute("SELECT current_setting('max_temp_directory_size')").fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.parametrize("read_only", [False, True])
def test_writer_and_reader_connections_cap_spills(tmp_path: Path, read_only: bool) -> None:
    db = tmp_path / "store.duckdb"
    connect(db).close()  # create the file so a read-only open works
    conn = connect(db, read_only=read_only)
    try:
        assert _temp_cap(conn) == "3.7 GiB"  # DuckDB reports 4GB (decimal) in GiB
    finally:
        conn.close()


def test_cursors_share_the_cap(tmp_path: Path) -> None:
    conn = connect(tmp_path / "store.duckdb")
    try:
        cur = conn.cursor()
        assert _temp_cap(cur) == "3.7 GiB"
        cur.close()
    finally:
        conn.close()


def test_a_query_that_outgrows_the_cap_fails_instead_of_filling_the_disk(tmp_path: Path) -> None:
    # Same mechanism, small scale: tiny memory and spill limits, then a sort that needs more.
    conn = connect(tmp_path / "store.duckdb")
    try:
        conn.execute("SET memory_limit = '32MB'")
        conn.execute("SET max_temp_directory_size = '8MB'")
        conn.execute("SET preserve_insertion_order = false")
        with pytest.raises(duckdb.OutOfMemoryException, match="max_temp_directory_size"):
            conn.execute(
                "SELECT * FROM (SELECT md5(i::VARCHAR) || md5((i * 7)::VARCHAR) AS s "
                "FROM range(3000000) t(i)) ORDER BY s"
            ).fetchall()
    finally:
        conn.close()
