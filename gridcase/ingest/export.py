"""Exports for the dbt warehouse (dbt/). The server is the only process that may open the store,
so the market metadata dbt needs (category, event) is written out as a Parquet file after a
refresh. The file is replaced atomically, so dbt never reads a half-written one.

The whole file is rewritten each time, so it is written only while something reads it: the
reader leaves an ask beside the file (`ask_for_markets`, called by the warehouse's freshness
gate), and the server exports while the newest ask is recent."""

from __future__ import annotations

import os
import time
from pathlib import Path

import duckdb


def export_markets(conn: duckdb.DuckDBPyConnection, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    conn.execute(f"COPY (SELECT * FROM markets ORDER BY condition_id) TO '{tmp}' (FORMAT parquet)")
    os.replace(tmp, path)
    row = conn.execute("SELECT count(*) FROM markets").fetchone()
    return int(row[0]) if row else 0


ASKED_FOR_S = 2 * 3600.0  # the warehouse build is hourly: two missed asks and the export stops


def _ask(path: Path) -> Path:
    return path.with_suffix(".asked")


def ask_for_markets(path: Path) -> None:
    """Called by whatever reads the export, each time it runs: keep it coming."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ask(path).touch()


def export_markets_if_asked(conn: duckdb.DuckDBPyConnection, path: Path) -> bool:
    ask = _ask(path)
    if not ask.exists() or time.time() - ask.stat().st_mtime > ASKED_FOR_S:
        return False
    export_markets(conn, path)
    return True
