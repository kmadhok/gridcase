"""Connection helpers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb

from gridcase.store.schema import create_schema

# DuckDB's default is 80% of the machine's RAM (about 9.3 GiB on kayak), and it keeps every block
# it reads until it reaches that. Each refresh scans columns of the whole trades table, so the
# server grew by a gigabyte within seconds. Measured on kayak's 51M trades: at this limit the
# process peaks at 1.3 GB and the refresh is no slower than at 3.1 GiB; big queries spill to
# `<store>.tmp`. Keep this below MemoryHigh in deploy/gridcase-server.service.
MEMORY_LIMIT = "1200MB"


def connect(path: Path | str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the store. Timestamps are naive UTC; TimeZone is pinned so nothing shifts."""
    p = Path(path)
    if not read_only:
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(p), read_only=read_only)
    conn.execute("SET TimeZone = 'UTC'")
    # One thread: parallel aggregates change floating-point summation order between runs,
    # and receipts must be byte-identical. Our queries are small; this costs nothing visible.
    conn.execute("SET threads = 1")
    conn.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
    if not read_only:
        create_schema(conn)
    return conn


def one(conn: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...]:
    row = conn.execute(sql, list(params)).fetchone()
    if row is None:
        raise LookupError(f"no row for: {sql}")
    return tuple(row)
