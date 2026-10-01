"""Exports for the dbt warehouse (dbt/). The server is the only process that may open the store,
so the market metadata dbt needs (category, event) is written out as a Parquet file after each
refresh. The file is replaced atomically, so dbt never reads a half-written one."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb


def export_markets(conn: duckdb.DuckDBPyConnection, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    conn.execute(f"COPY (SELECT * FROM markets ORDER BY condition_id) TO '{tmp}' (FORMAT parquet)")
    os.replace(tmp, path)
    row = conn.execute("SELECT count(*) FROM markets").fetchone()
    return int(row[0]) if row else 0
