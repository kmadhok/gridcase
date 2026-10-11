# ruff: noqa: E501  (SQL and table rows read better unwrapped)
"""Phase 2/3 sampling frame: every closed market that ended in the window, from Gamma.

Pages through gamma-api /markets/keyset (closed=true, end_date in [--start, --end), volume >= $1k,
largest first) and writes one row per market to data/feasibility/backfill.duckdb `census`.
Metadata only: no prices, no trades. Recurring short-horizon markets ("Up or Down" crypto
5/15-minute and hourly series) are flagged, not dropped, so their share can be reported.

    uv run python experiments/feasibility/census.py --start 2026-04-11 --end 2026-10-11
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import duckdb
from api import GAMMA, ROOT, Api

DB = Path(os.environ.get("FEASIBILITY_DB") or ROOT / "data" / "feasibility" / "backfill.duckdb")
RECURRING = re.compile(
    r"up or down|updown|\b\d{1,2}(:\d\d)?\s?(am|pm)[- ]\d{1,2}(:\d\d)?\s?(am|pm) et", re.I
)


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def row(m: dict[str, Any]) -> tuple[Any, ...]:
    events = m.get("events") or [{}]
    ev = events[0]
    tags = "|".join(t.get("label", "") for t in (m.get("tags") or ev.get("tags") or []))
    prices = [_f(p) for p in json.loads(m.get("outcomePrices") or "[]")]
    outcomes = json.loads(m.get("outcomes") or "[]")
    tokens = json.loads(m.get("clobTokenIds") or "[]")
    q = m.get("question") or ""
    winner = None
    if len(prices) == 2 and sorted(prices) == [0.0, 1.0]:
        winner = prices.index(1.0)
    return (
        m.get("conditionId"),
        str(m.get("id")),
        q,
        m.get("slug"),
        str(ev.get("id")) if ev.get("id") else None,
        ev.get("slug"),
        ev.get("title"),
        tags,
        _f(m.get("volumeNum")),
        m.get("startDate"),
        m.get("endDate"),
        m.get("closedTime"),
        json.dumps(outcomes),
        json.dumps(prices),
        winner,
        m.get("umaResolutionStatus"),
        bool(m.get("negRisk")),
        tokens[0] if tokens else None,
        tokens[1] if len(tokens) > 1 else None,
        bool(RECURRING.search(q + " " + (m.get("slug") or ""))),
    )


DDL = """
CREATE OR REPLACE TABLE census (
    condition_id VARCHAR, market_id VARCHAR, question VARCHAR, slug VARCHAR,
    event_id VARCHAR, event_slug VARCHAR, event_title VARCHAR, tags VARCHAR,
    volume DOUBLE, start_date VARCHAR, end_date VARCHAR, closed_time VARCHAR,
    outcomes VARCHAR, outcome_prices VARCHAR, winner_index INTEGER, uma_status VARCHAR,
    neg_risk BOOLEAN, token0 VARCHAR, token1 VARCHAR, recurring BOOLEAN
)
"""


def run(start: str, end: str, min_volume: float, max_pages: int, db: Path = DB) -> None:
    api = Api()
    rows: list[tuple[Any, ...]] = []
    cursor = None
    pages = 0
    while pages < max_pages:
        body = api.get(
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
        markets = body.get("markets") or []
        rows += [row(m) for m in markets if m.get("conditionId")]
        pages += 1
        cursor = body.get("next_cursor")
        if pages % 25 == 0:
            print(
                f"  {pages} pages, {len(rows)} markets, last volume {rows[-1][8]:,.0f}", flush=True
            )
        if not cursor:
            break
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    con.execute(DDL)
    con.executemany("INSERT INTO census VALUES (" + ",".join("?" * 20) + ")", rows)
    con.execute("CREATE OR REPLACE TABLE census AS SELECT DISTINCT ON (condition_id) * FROM census")
    n = con.execute("SELECT count(*) FROM census").fetchone()
    print(
        f"census: {n[0] if n else 0} markets, {pages} pages, complete={cursor is None}", api.stats()
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-04-11")
    ap.add_argument("--end", default="2026-10-11")
    ap.add_argument("--min-volume", type=float, default=1000)
    ap.add_argument("--max-pages", type=int, default=2000)
    a = ap.parse_args()
    run(a.start, a.end, a.min_volume, a.max_pages)
