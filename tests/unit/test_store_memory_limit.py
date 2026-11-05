"""The store caps DuckDB's memory: its default, 80% of RAM, let the server take over kayak."""

from __future__ import annotations

from pathlib import Path

from gridcase.store.db import connect, one


def _limit_mib(path: Path, read_only: bool = False) -> float:
    conn = connect(path, read_only=read_only)
    try:
        return (
            float(
                one(conn, "SELECT value FROM duckdb_settings() WHERE name = 'memory_limit'")[
                    0
                ].split()[0]
            )
            * 1024
        )
    finally:
        conn.close()


def test_writer_and_reader_connections_are_capped(tmp_path: Path) -> None:
    db = tmp_path / "store.duckdb"
    assert 1100 <= _limit_mib(db) <= 1200  # DuckDB reports 1200MB as "1.1 GiB"
    assert 1100 <= _limit_mib(db, read_only=True) <= 1200


def test_cursors_share_the_cap(tmp_path: Path) -> None:
    conn = connect(tmp_path / "store.duckdb")
    try:
        cur = conn.cursor()
        assert cur.execute("SELECT current_setting('memory_limit')").fetchone() == ("1.1 GiB",)
        cur.close()
    finally:
        conn.close()
