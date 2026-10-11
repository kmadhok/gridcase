# ruff: noqa: E501  (SQL and table rows read better unwrapped)
"""Fetch helpers for the feasibility backfill: price series, trades and resolutions.

Findings these encode (docs.polymarket.com checked 2026-10-11, then tested live):
- CLOB /prices-history takes `market=<token id>`, `startTs`, `endTs`, `fidelity` (minutes).
  Any window longer than ~15 days is a 400 ("interval is too long"), whatever the fidelity,
  so long lives are fetched in 14-day chunks. `interval=max` returns nothing for closed
  markets. 1-minute points exist for markets that closed in 2023.
- data-api /v2/prices-history is the documented successor, but for closed markets it serves
  only 12-hour points (asking for finer buckets returns the same or an empty page).
- data-api /v2/trades?condition= walks a market's whole trade history (cursor pagination,
  1,000 trades a page, taker side only by default).
- data-api /v2/resolutions?condition= (up to 20 ids) gives UMA status and the settled price.
"""

from __future__ import annotations

from typing import Any

from api import CLOB, DATA, Api

CHUNK_S = 14 * 86400


def clob_prices(
    api: Api, token: str, start: int, end: int, fidelity_min: int
) -> list[tuple[int, float]]:
    """(ts, price) oldest first over [start, end], in 14-day chunks."""
    out: dict[int, float] = {}
    lo = start
    while lo < end:
        hi = min(lo + CHUNK_S, end)
        body = api.get(
            CLOB + "/prices-history",
            {"market": token, "startTs": lo, "endTs": hi, "fidelity": fidelity_min},
        )
        for pt in (body.get("history") or []) if isinstance(body, dict) else []:
            out[int(pt["t"])] = float(pt["p"])
        lo = hi
    return sorted(out.items())


def v2_prices_max(api: Api, token: str) -> list[dict[str, Any]]:
    body = api.get(DATA + "/v2/prices-history", {"token_id": token, "interval": "max"})
    return list(body.get("data") or [])


def v2_trades(api: Api, condition: str, max_pages: int) -> tuple[list[dict[str, Any]], bool]:
    """All trades of one market, newest first; (rows, complete)."""
    rows: list[dict[str, Any]] = []
    cursor = None
    for _ in range(max_pages):
        body = api.get(
            DATA + "/v2/trades", {"condition": condition, "limit": 1000, "cursor": cursor}
        )
        rows += body.get("data") or []
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if not cursor:
            return rows, True
    return rows, False


def v2_resolutions(api: Api, conditions: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(conditions), 20):
        body = api.get(DATA + "/v2/resolutions", {"condition": ",".join(conditions[i : i + 20])})
        for r in body.get("data") or []:
            out[r["condition_id"]] = r
    return out
