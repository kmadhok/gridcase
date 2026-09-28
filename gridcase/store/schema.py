"""DDL for the bitemporal store and the as_of table macro."""

from __future__ import annotations

import duckdb

DDL = [
    """
    CREATE TABLE IF NOT EXISTS observations (
        series_id    VARCHAR NOT NULL,
        entity_id    VARCHAR NOT NULL,
        valid_time   TIMESTAMP NOT NULL,
        value        DOUBLE,
        observed_at  TIMESTAMP NOT NULL,
        available_at TIMESTAMP NOT NULL,
        source       VARCHAR NOT NULL,
        vintage      VARCHAR NOT NULL DEFAULT 'observed',
        raw_hash     VARCHAR,
        PRIMARY KEY (series_id, entity_id, valid_time, observed_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS series (
        series_id     VARCHAR PRIMARY KEY,
        name          VARCHAR NOT NULL,
        unit          VARCHAR NOT NULL,
        source        VARCHAR NOT NULL,
        lag_hours     INTEGER NOT NULL,
        history_start TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entities (
        entity_id VARCHAR PRIMARY KEY,
        kind      VARCHAR NOT NULL,
        name      VARCHAR NOT NULL,
        parent_id VARCHAR,
        tz        VARCHAR,
        lat       DOUBLE,
        lon       DOUBLE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_runs (
        run_id       VARCHAR PRIMARY KEY,
        source       VARCHAR NOT NULL,
        started_at   TIMESTAMP NOT NULL,
        finished_at  TIMESTAMP,
        rows_written INTEGER NOT NULL DEFAULT 0,
        status       VARCHAR NOT NULL,
        error        VARCHAR
    )
    """,
    # Latest known row per key with available_at <= t. Ties on available_at (two synthetic
    # rows) resolve to the later observed_at.
    """
    CREATE OR REPLACE MACRO as_of(t) AS TABLE
    SELECT series_id, entity_id, valid_time, value, observed_at, available_at,
           source, vintage, raw_hash
    FROM observations
    WHERE available_at <= t
    QUALIFY row_number() OVER (
        PARTITION BY series_id, entity_id, valid_time
        ORDER BY available_at DESC, observed_at DESC
    ) = 1
    """,
]


def create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    for stmt in DDL:
        conn.execute(stmt)
