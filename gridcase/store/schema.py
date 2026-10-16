"""DDL for the MarketCase store: trades and markets, the case ledger, and trades_as_of."""

from __future__ import annotations

import duckdb

DDL = [
    """
    CREATE TABLE IF NOT EXISTS case_trace (
        case_id     VARCHAR NOT NULL,
        seq         INTEGER NOT NULL,
        parent_seq  INTEGER,
        kind        VARCHAR NOT NULL CHECK (kind IN ('stage', 'tool', 'llm')),
        name        VARCHAR NOT NULL,
        started_at  TIMESTAMP NOT NULL,
        ms          DOUBLE NOT NULL,
        status      VARCHAR NOT NULL CHECK (status IN ('ok', 'error')),
        detail_json VARCHAR NOT NULL,
        PRIMARY KEY (case_id, seq)
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
    # The daily digest (gridcase/digest.py): one row per day; its receipts are in the ledger
    # under case_id 'digest-<day>'.
    """
    CREATE TABLE IF NOT EXISTS digests (
        day         DATE PRIMARY KEY,
        as_of       TIMESTAMP NOT NULL,
        created_at  TIMESTAMP NOT NULL,
        digest_json VARCHAR NOT NULL
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
]


# Polymarket (MarketCase). trades: one row per fill, deduplicated on trade_id = hash of
# (transactionHash, asset, side, size, price); ts is when it traded, collected_at when our
# collector saw it (what trades_as_of filters on, so a replay never sees later knowledge).
POLYMARKET_DDL = [
    """
    CREATE TABLE IF NOT EXISTS trades (
        trade_id      VARCHAR NOT NULL,
        ts            TIMESTAMP NOT NULL,
        wallet        VARCHAR NOT NULL,
        side          VARCHAR,
        size          DOUBLE NOT NULL,
        price         DOUBLE NOT NULL,
        usd           DOUBLE NOT NULL,
        asset         VARCHAR,
        condition_id  VARCHAR NOT NULL,
        market_slug   VARCHAR,
        event_slug    VARCHAR,
        outcome       VARCHAR,
        title         VARCHAR,
        tx_hash       VARCHAR,
        collected_at  TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS markets (
        condition_id  VARCHAR PRIMARY KEY,
        question      VARCHAR,
        event_slug    VARCHAR,
        event_title   VARCHAR,
        category      VARCHAR NOT NULL,
        tags          VARCHAR,
        fetched_at    TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collector_gaps (
        gap_from     TIMESTAMP NOT NULL,
        gap_to       TIMESTAMP NOT NULL,
        detected_at  TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_files (
        path   VARCHAR PRIMARY KEY,
        size   BIGINT NOT NULL,
        mtime  DOUBLE NOT NULL
    )
    """,
    # The Now page and the KPI catalog as last computed, so a restart serves them at once.
    """
    CREATE TABLE IF NOT EXISTS page_snapshots (
        name   VARCHAR PRIMARY KEY,
        as_of  TIMESTAMP NOT NULL,
        body   VARCHAR NOT NULL
    )
    """,
    """
    CREATE OR REPLACE MACRO trades_as_of(t) AS TABLE
    SELECT * FROM trades WHERE collected_at <= t AND ts <= t
    """,
]

MIGRATIONS = [
    "ALTER TABLE cases ADD COLUMN IF NOT EXISTS step_errors_json VARCHAR",
]


def create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    for stmt in [*DDL, *POLYMARKET_DDL, *MIGRATIONS]:
        conn.execute(stmt)
