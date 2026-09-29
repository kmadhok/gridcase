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
    # Evidence ledger: append-only receipts per case (gridcase/ledger).
    """
    CREATE TABLE IF NOT EXISTS ledger (
        case_id      VARCHAR NOT NULL,
        receipt_id   VARCHAR NOT NULL,
        tool         VARCHAR NOT NULL,
        as_of        TIMESTAMP NOT NULL,
        created_at   TIMESTAMP NOT NULL,
        receipt_json VARCHAR NOT NULL,
        PRIMARY KEY (case_id, receipt_id)
    )
    """,
    # Case memory (gridcase/memory) and the one scheduled action type (re-check).
    """
    CREATE TABLE IF NOT EXISTS cases (
        case_id         VARCHAR PRIMARY KEY,
        entity          VARCHAR NOT NULL,
        metric          VARCHAR NOT NULL,
        opened_at       TIMESTAMP NOT NULL,
        as_of           TIMESTAMP NOT NULL,
        state           VARCHAR NOT NULL,
        season          VARCHAR NOT NULL,
        top_hypothesis  VARCHAR,
        plan_json       VARCHAR,
        conclusion_json VARCHAR,
        plan_attempts   INTEGER NOT NULL DEFAULT 0,
        llm_calls       INTEGER NOT NULL DEFAULT 0,
        llm_usage_json  VARCHAR,
        verdicts_json   VARCHAR,
        recheck_json    VARCHAR,
        action_json     VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        task_id    VARCHAR PRIMARY KEY,
        case_id    VARCHAR NOT NULL,
        kind       VARCHAR NOT NULL,
        run_at     TIMESTAMP NOT NULL,
        status     VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS learned_priors (
        template_id VARCHAR NOT NULL,
        ba          VARCHAR NOT NULL,
        season      VARCHAR NOT NULL,
        a_delta     DOUBLE NOT NULL DEFAULT 0,
        b_delta     DOUBLE NOT NULL DEFAULT 0,
        PRIMARY KEY (template_id, ba, season)
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


MIGRATIONS = [
    "ALTER TABLE cases ADD COLUMN IF NOT EXISTS step_errors_json VARCHAR",
]


def create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    for stmt in [*DDL, *MIGRATIONS]:
        conn.execute(stmt)
