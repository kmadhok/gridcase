"""Freshness gate and publication of dbt's small marts for Hex."""

from __future__ import annotations

import os
import time

import duckdb

from gridcase import config

MARTS = ("metrics_daily", "metrics_hourly_by_category", "dim_markets")


def check_sources(max_age_minutes: float = 15) -> int:
    """Check file mtimes, not trade timestamps: only a live collector can extend history."""
    raw = config.RAW_DIR / "polymarket" / "trades"
    mtimes = [p.stat().st_mtime for p in raw.glob("*/*/*/*.jsonl.gz") if p.is_file()]
    if not mtimes:
        print("sources missing: raw trade files")
        return 1
    now = time.time()
    if now - max(mtimes) > max_age_minutes * 60:
        print(f"sources stale: raw trade files older than {max_age_minutes:g} minutes")
        return 1
    export = config.EXPORT_DIR / "markets.parquet"
    if not export.is_file():
        print("sources missing: markets.parquet")
        return 1
    if now - export.stat().st_mtime > max_age_minutes * 60:
        print(f"sources stale: markets.parquet older than {max_age_minutes:g} minutes")
        return 1
    return 0


def publish() -> int:
    """Replace the three marts together; attach the local dbt warehouse read-only."""
    target = os.environ.get("MARKETCASE_PUBLISH_TARGET", "md:marketcase")
    motherduck = target.startswith("md:")
    token = os.environ.get("MOTHERDUCK_TOKEN")
    if motherduck and not token:
        print("publish skipped: no MOTHERDUCK_TOKEN")
        return 0

    # MotherDuck reads this lowercase variable. Keep credentials out of connection strings
    # (including exceptions), and restore the caller's environment after publication.
    previous = os.environ.get("motherduck_token")  # noqa: SIM112 - MotherDuck's variable
    if motherduck and token:
        os.environ["motherduck_token"] = token  # noqa: SIM112
    try:
        with duckdb.connect("md:" if motherduck else target) as conn:
            if motherduck:
                conn.execute("CREATE DATABASE IF NOT EXISTS marketcase")
                conn.execute("USE marketcase")
            source = str(config.WAREHOUSE_PATH).replace("'", "''")
            conn.execute(f"ATTACH '{source}' AS source_warehouse (READ_ONLY)")
            conn.execute("BEGIN TRANSACTION")
            counts: list[tuple[str, int]] = []
            for name in MARTS:
                conn.execute(
                    f"CREATE OR REPLACE TABLE {name} AS "
                    f"SELECT * FROM source_warehouse.main_marts.{name}"
                )
                row = conn.execute(f"SELECT count(*) FROM {name}").fetchone()
                assert row is not None
                counts.append((name, int(row[0])))
            conn.execute("COMMIT")
            for name, count in counts:
                print(f"{name}: {count} rows")
    except duckdb.Error:
        # Extension errors can contain authentication details. Never echo them into task logs.
        print("publish failed: could not copy warehouse marts")
        return 1
    finally:
        if motherduck:
            if previous is None:
                os.environ.pop("motherduck_token", None)
            else:
                os.environ["motherduck_token"] = previous  # noqa: SIM112
    return 0
