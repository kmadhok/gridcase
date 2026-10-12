"""Fetch helpers: price series, trades, resolutions, and a market's trading life.

What these encode (docs.polymarket.com read 2026-10-11, then tested live; FEASIBILITY.md §2):
- CLOB /prices-history takes `market=<token id>`, `startTs`, `endTs`, `fidelity` (minutes).
  A window longer than ~15 days is a 400 at any fidelity, so lives are fetched in 14-day chunks;
  `interval=max` returns nothing for closed markets. 1-minute points exist back to 2023.
- data-api /v2/trades?condition= walks a market's whole trade history newest first (cursor
  pagination, 1,000 a page, taker side only by default; the condition shape ignores start/end).
- data-api /v2/resolutions?condition= (up to 20 ids): UMA rows carry `price` (1e18 = outcome 0),
  native v2 rows carry `payouts`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from gridcase.backfill.client import CLOB, DATA, Client

CHUNK_S = 14 * 86400


def parse_ts(s: str | None) -> int | None:
    """Epoch seconds from Gamma's assorted timestamp spellings, or None."""
    if not s:
        return None
    s = s.strip().replace(" ", "T").replace("Z", "+00:00")
    if s.endswith(("+00", "-00")):
        s += ":00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        return None
    return int(d.timestamp())


def life(start: int | None, end: int | None, closed: int | None) -> tuple[int, int] | None:
    """[start, stop) of the trading we use: up to the earlier of the scheduled end and the actual
    close (later trading is mostly convergence while resolution is pending). Gamma sometimes has
    an end before the start (a market created after its nominal end): then the close."""
    if start is None:
        return None
    ends = [x for x in (closed, end) if x is not None and x > start]
    return (start, min(ends)) if ends else None


def clob_prices(
    client: Client, token: str, start: int, stop: int, fidelity_min: int
) -> list[tuple[int, float]]:
    """(ts, price) oldest first over [start, stop], in 14-day chunks."""
    out: dict[int, float] = {}
    lo = start
    while lo < stop:
        hi = min(lo + CHUNK_S, stop)
        body = client.get(
            CLOB + "/prices-history",
            {"market": token, "startTs": lo, "endTs": hi, "fidelity": fidelity_min},
        )
        for pt in body.get("history") or [] if isinstance(body, dict) else []:
            out[int(pt["t"])] = float(pt["p"])
        lo = hi
    return sorted(out.items())


def v2_trades(
    client: Client, condition: str, stop_before: int | None, max_pages: int
) -> tuple[list[dict[str, Any]], bool]:
    """Trades of one market, newest first, until the walk passes `stop_before` (epoch s) or
    the history ends. Returns (rows, complete): complete is False only when max_pages ran out
    before reaching `stop_before` or the first trade."""
    rows: list[dict[str, Any]] = []
    cursor = None
    for _ in range(max_pages):
        body = client.get(
            DATA + "/v2/trades", {"condition": condition, "limit": 1000, "cursor": cursor}
        )
        if not isinstance(body, dict):
            return rows, True
        page = body.get("data") or []
        rows += page
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if not cursor:
            return rows, True
        if stop_before is not None and page and int(page[-1]["timestamp"]) < stop_before:
            return rows, True
    return rows, False


def settled_token0(r: dict[str, Any]) -> float | None:
    """Token 0's settled price from a /v2/resolutions row."""
    if r.get("price") not in (None, ""):
        return int(r["price"]) / 1e18
    pay = r.get("payouts")
    if pay and sum(pay):
        return float(pay[0]) / float(sum(pay))
    return None


def v2_resolutions(client: Client, conditions: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(conditions), 20):
        body = client.get(DATA + "/v2/resolutions", {"condition": ",".join(conditions[i : i + 20])})
        for r in body.get("data") or [] if isinstance(body, dict) else []:
            out[r["condition_id"]] = r
    return out
