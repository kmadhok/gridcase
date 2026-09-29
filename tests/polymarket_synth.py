"""Tiny helpers to build Polymarket stores by hand for tests (exact, readable numbers)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import duckdb


def add_trades(conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
    """Each row: ts, wallet, usd (price defaults to 0.5, so size = usd / price), and optional
    condition_id, side, title, event_slug, collected_at (default ts + 2 minutes)."""
    for i, r in enumerate(rows):
        price = float(r.get("price", 0.5))
        usd = float(r["usd"])
        ts: datetime = r["ts"]
        conn.execute(
            "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                r.get("trade_id", f"t{len(rows)}-{i}-{ts.isoformat()}-{r['wallet']}-{usd}"),
                ts,
                r["wallet"],
                r.get("side", "BUY"),
                usd / price,
                price,
                usd,
                "asset",
                r.get("condition_id", "c1"),
                "slug",
                r.get("event_slug", "ev"),
                "Yes",
                r.get("title", "Will it happen?"),
                "tx",
                r.get("collected_at", ts + timedelta(minutes=2)),
            ],
        )


def add_market(
    conn: duckdb.DuckDBPyConnection,
    condition_id: str,
    category: str,
    event_title: str = "An event",
    question: str = "Will it happen?",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO markets VALUES (?, ?, ?, ?, ?, ?, ?)",
        [condition_id, question, "ev", event_title, category, "", datetime(2026, 1, 1)],
    )
