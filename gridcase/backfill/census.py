"""The closed-market census: every resolved binary market that ended in a window, from Gamma.

Pages gamma-api /markets/keyset (closed=true, end_date in [start, end), volume >= min, largest
first; Gamma refuses offsets >= 10,000 and the documented `order=volume_num` - `volumeNum`
works). Keeps binary markets whose outcome prices settled at exactly 0 and 1, and drops
recurring short-horizon series ("Up or Down" crypto windows). Categories come from
ontology/categories.yaml via the collector's own `categorize`, so they match the live store.
"""

from __future__ import annotations

import json
import re
from typing import Any

from gridcase.backfill.client import GAMMA, Client
from gridcase.backfill.fetch import parse_ts
from gridcase.ingest.polymarket_load import categorize

RECURRING = re.compile(
    r"up or down|updown|\b\d{1,2}(:\d\d)?\s?(am|pm)[- ]\d{1,2}(:\d\d)?\s?(am|pm) et", re.I
)


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def market_row(m: dict[str, Any]) -> dict[str, Any] | None:
    """One census row, or None for a market outside the frame (not binary, not settled 0/1,
    recurring, no tokens). The winner is kept in its own fields so tools can drop them."""
    q = m.get("question") or ""
    if RECURRING.search(f"{q} {m.get('slug') or ''}"):
        return None
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = [_f(p) for p in json.loads(m.get("outcomePrices") or "[]")]
        tokens = json.loads(m.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    if len(outcomes) != 2 or len(tokens) != 2 or sorted(p or 0.0 for p in prices) != [0.0, 1.0]:
        return None
    if None in prices or not m.get("conditionId"):
        return None
    ev = (m.get("events") or [{}])[0]
    tags = [t.get("label", "") for t in (m.get("tags") or ev.get("tags") or [])]
    return {
        "condition_id": m["conditionId"],
        "market_id": str(m.get("id")),
        "question": q,
        "slug": m.get("slug"),
        "event_id": str(ev["id"]) if ev.get("id") else None,
        "event_slug": ev.get("slug"),
        "event_title": ev.get("title"),
        "tags": "|".join(t for t in tags if t),
        "category": categorize(tags, f"{ev.get('title') or ''} {q}"),
        "volume": _f(m.get("volumeNum")) or 0.0,
        "start_ts": parse_ts(m.get("startDate")),
        "end_ts": parse_ts(m.get("endDate")),
        "closed_ts": parse_ts(m.get("closedTime")),
        "neg_risk": bool(m.get("negRisk")),
        "token0": tokens[0],
        "token1": tokens[1],
        "outcome0": outcomes[0],
        "outcome1": outcomes[1],
        "winner_index": prices.index(1.0),
        "uma_status": m.get("umaResolutionStatus"),
    }


def fetch_census(
    client: Client, start: str, end: str, min_volume: float, max_pages: int = 5000
) -> tuple[list[dict[str, Any]], int, bool]:
    """(rows, pages read, complete)."""
    rows: dict[str, dict[str, Any]] = {}
    cursor = None
    pages = 0
    while pages < max_pages:
        body = client.get(
            GAMMA + "/markets/keyset",
            {
                "closed": "true",
                "end_date_min": f"{start}T00:00:00Z",
                "end_date_max": f"{end}T00:00:00Z",
                "volume_num_min": min_volume,
                "order": "volumeNum",
                "ascending": "false",
                "limit": 100,
                "include_tag": "true",
                "after_cursor": cursor,
            },
        )
        pages += 1
        for m in body.get("markets") or []:
            r = market_row(m)
            if r is not None:
                rows.setdefault(r["condition_id"], r)
        cursor = body.get("next_cursor")
        if not cursor:
            return list(rows.values()), pages, True
    return list(rows.values()), pages, False
