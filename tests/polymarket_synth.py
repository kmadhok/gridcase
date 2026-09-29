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


# A synthetic 9-day market: six markets in three categories, 40 returning wallets, ordinary
# trade sizes at even odds, a small day-to-day wobble. Investigations run at AS_OF over the
# last six complete hours (WINDOW_START..AS_OF hour).
START = datetime(2026, 10, 1)
AS_OF = START + timedelta(days=9, hours=14, minutes=20)
WINDOW_START = START + timedelta(days=9, hours=8)
MARKETS = {
    "m1": ("Sports", "England vs Czechia"),
    "m2": ("Sports", "Lakers vs Celtics"),
    "m3": ("Crypto", "Bitcoin up or down"),
    "m4": ("Crypto", "Ethereum above $5k"),
    "m5": ("Politics", "Fed cuts rates"),
    "m6": ("Politics", "Election winner"),
}


def base_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for h in range(9 * 24 + 15):
        t = START + timedelta(hours=h)
        wobble = 1.0 + 0.05 * ((h // 24) % 3 - 1)
        for i, cid in enumerate(MARKETS):
            for k in range(4):
                rows.append(
                    {
                        "ts": t + timedelta(minutes=3 * k + i),
                        "wallet": f"w{(h + i + k) % 40}",
                        "usd": (60.0 if k % 2 else 240.0) * wobble,
                        "condition_id": cid,
                        "trade_id": f"b{h}-{cid}-{k}",
                    }
                )
    return rows


def window_hours() -> list[datetime]:
    return [WINDOW_START + timedelta(hours=i) for i in range(6)]


def build_market_store(conn: duckdb.DuckDBPyConnection) -> None:
    for cid, (cat, title) in MARKETS.items():
        add_market(conn, cid, cat, title)
    add_trades(conn, base_rows())


def one_market_tripled() -> list[dict[str, Any]]:
    """Extra trading in England vs Czechia over the window: its volume about triples."""
    return [
        {
            "ts": h + timedelta(minutes=30 + k),
            "wallet": f"w{k}",
            "usd": 600.0,
            "condition_id": "m1",
            "trade_id": f"x{h}-{k}",
        }
        for h in window_hours()
        for k in range(4)
    ]
