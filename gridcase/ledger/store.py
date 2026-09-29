"""Append-only ledger table access."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from gridcase.ledger.schema import Receipt


def write_receipt(conn: duckdb.DuckDBPyConnection, case_id: str, receipt: Receipt) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ledger (case_id, receipt_id, tool, as_of, created_at, receipt_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            case_id,
            receipt.receipt_id,
            receipt.tool,
            receipt.as_of,
            datetime.now(UTC).replace(tzinfo=None),
            receipt.model_dump_json(),
        ],
    )


def read_receipts(conn: duckdb.DuckDBPyConnection, case_id: str) -> list[Receipt]:
    rows = conn.execute(
        "SELECT receipt_json FROM ledger WHERE case_id = ? ORDER BY created_at, receipt_id",
        [case_id],
    ).fetchall()
    return [Receipt.model_validate_json(r[0]) for r in rows]


def get_receipt(conn: duckdb.DuckDBPyConnection, receipt_id: str) -> Receipt:
    row = conn.execute(
        "SELECT receipt_json FROM ledger WHERE receipt_id = ? LIMIT 1", [receipt_id]
    ).fetchone()
    if row is None:
        raise KeyError(receipt_id)
    return Receipt.model_validate_json(row[0])
