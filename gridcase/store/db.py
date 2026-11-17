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

# Spill files in `<store>.tmp` were unbounded: DuckDB's default allows 90% of the free space it
# sees, and on kayak Linux reports ~800 GB free while the Windows C: drive holding Linux's disk
# file has a few GB. A cold start (28 days of watch values, then the page rebuild) wrote ~22 GB
# in total on 2026-11-16 while C: went toward empty, which has crashed WSL before. With this
# cap a query that needs more fails with an out-of-memory error instead; the background loop
# logs it and tries again later. See ~/kayak-ops/notes/disk-space-plan.md on kayak.
MAX_TEMP_DIRECTORY_SIZE = "4GB"


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
    conn.execute(f"SET max_temp_directory_size = '{MAX_TEMP_DIRECTORY_SIZE}'")
    if not read_only:
        create_schema(conn)
    return conn


def one(conn: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...]:
    row = conn.execute(sql, list(params)).fetchone()
    if row is None:
        raise LookupError(f"no row for: {sql}")
    return tuple(row)
