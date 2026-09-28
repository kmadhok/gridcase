"""Connection, append-only writes, and as_of reads."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from gridcase.store.models import Observation, to_naive_utc
from gridcase.store.schema import create_schema
from gridcase.store.seed import seed_entities, seed_series

OBS_COLS = (
    "series_id, entity_id, valid_time, value, observed_at, available_at, source, vintage, raw_hash"
)


def connect(path: Path | str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the store. Timestamps are naive UTC; TimeZone is pinned so nothing shifts."""
    p = Path(path)
    if not read_only:
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(p), read_only=read_only)
    conn.execute("SET TimeZone = 'UTC'")
    if not read_only:
        create_schema(conn)
        seed_series(conn)
        seed_entities(conn)
    return conn


def one(conn: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...]:
    row = conn.execute(sql, list(params)).fetchone()
    if row is None:
        raise LookupError(f"no row for: {sql}")
    return tuple(row)


def append_observations(
    conn: duckdb.DuckDBPyConnection,
    rows: Sequence[Observation],
    dedupe_unchanged: bool = True,
) -> int:
    """Append rows. Never updates or deletes.

    With dedupe_unchanged (the ingestion default), a row is written only if the key is new
    or its value differs from the latest known value. A changed value for a key that already
    exists is a real revision seen now, so it is stamped observed/observed_at regardless of
    what the caller proposed for available_at/vintage.
    """
    if not rows:
        return 0
    conn.execute("CREATE OR REPLACE TEMP TABLE staged AS SELECT * FROM observations LIMIT 0")
    conn.executemany(
        f"INSERT INTO staged ({OBS_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [
                r.series_id,
                r.entity_id,
                r.valid_time,
                r.value,
                r.observed_at,
                r.available_at,
                r.source,
                r.vintage,
                r.raw_hash,
            ]
            for r in rows
        ],
    )
    before = int(one(conn, "SELECT count(*) FROM observations")[0])
    if dedupe_unchanged:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO observations ({OBS_COLS})
            SELECT s.series_id, s.entity_id, s.valid_time, s.value, s.observed_at,
                   CASE WHEN l.value IS NULL THEN s.available_at ELSE s.observed_at END,
                   s.source,
                   CASE WHEN l.value IS NULL THEN s.vintage ELSE 'observed' END,
                   s.raw_hash
            FROM staged s
            LEFT JOIN (
                SELECT series_id, entity_id, valid_time, value
                FROM observations
                QUALIFY row_number() OVER (
                    PARTITION BY series_id, entity_id, valid_time
                    ORDER BY available_at DESC, observed_at DESC
                ) = 1
            ) l USING (series_id, entity_id, valid_time)
            WHERE l.value IS NULL OR l.value IS DISTINCT FROM s.value
            """
        )
    else:
        conn.execute(
            f"INSERT OR IGNORE INTO observations ({OBS_COLS}) SELECT {OBS_COLS} FROM staged"
        )
    conn.execute("DROP TABLE staged")
    after = int(one(conn, "SELECT count(*) FROM observations")[0])
    return after - before


def as_of(
    conn: duckdb.DuckDBPyConnection,
    t: datetime,
    series_id: str | None = None,
    entity_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Observation]:
    """Rows known at t, latest version per key; optional key and valid_time [start, end) filters."""
    where: list[str] = []
    params: list[Any] = [to_naive_utc(t)]
    for col, val in (("series_id", series_id), ("entity_id", entity_id)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    if start is not None:
        where.append("valid_time >= ?")
        params.append(to_naive_utc(start))
    if end is not None:
        where.append("valid_time < ?")
        params.append(to_naive_utc(end))
    sql = f"SELECT {OBS_COLS} FROM as_of(?::TIMESTAMP)"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY series_id, entity_id, valid_time"
    cols = [c.strip() for c in OBS_COLS.split(",")]
    return [
        Observation(**dict(zip(cols, row, strict=True)))
        for row in conn.execute(sql, params).fetchall()
    ]
