# ruff: noqa: E501  (SQL and table rows read better unwrapped)
"""Phase 2: what history can be backfilled? A bounded probe on 20 resolved, high-volume markets.

Picks from `census` (run census.py first): resolved binary markets that ended in the window,
not recurring "Up or Down" series, one market per event, largest volume first. For each:
the CLOB 1-minute and 60-minute series over the market's whole life, the v2 price history,
the v2 trade history (walked up to --max-trade-pages), and the v2 resolution, compared with
Gamma's settled outcome prices. Writes data/feasibility/probe.json and the `probe` and
`prices_probe` tables.

    uv run python experiments/feasibility/probe.py --n 20
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Any

import duckdb
from api import ROOT, Api
from backfill import clob_prices, v2_prices_max, v2_resolutions, v2_trades
from census import DB

OUT = ROOT / "data" / "feasibility" / "probe.json"


def ts(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    return int(datetime.fromisoformat(s).timestamp())


def life(start: str | None, end: str | None, closed: str | None) -> tuple[int, int]:
    lo = ts(start) or 0
    hi = min(x for x in (ts(closed), ts(end)) if x) if (closed or end) else lo
    return lo, max(hi, lo)


def run(n: int, max_trade_pages: int) -> None:
    con = duckdb.connect(str(DB))
    picks = con.execute(
        """
        SELECT condition_id, question, event_slug, volume, start_date, end_date, closed_time,
               winner_index, token0, token1
        FROM census
        WHERE NOT recurring AND winner_index IS NOT NULL AND uma_status = 'resolved'
          AND token0 IS NOT NULL AND start_date IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY coalesce(event_id, condition_id) ORDER BY volume DESC) = 1
        ORDER BY volume DESC LIMIT ?
        """,
        [n],
    ).fetchall()
    api = Api()
    res = v2_resolutions(api, [p[0] for p in picks])
    results: list[dict[str, Any]] = []
    price_rows: list[tuple[Any, ...]] = []
    for cid, q, ev, vol, sd, ed, ct, winner, tok0, _tok1 in picks:
        lo, hi = life(sd, ed, ct)
        before = api.requests
        p1 = clob_prices(api, tok0, lo, hi, 1)
        r1 = api.requests - before
        before = api.requests
        p60 = clob_prices(api, tok0, lo, hi, 60)
        r60 = api.requests - before
        v2 = v2_prices_max(api, tok0)
        before = api.requests
        trades, complete = v2_trades(api, cid, max_trade_pages)
        rt = api.requests - before
        r = res.get(cid) or {}
        v2_price = int(r["price"]) / 1e18 if r.get("price") not in (None, "") else None
        # token0 is outcome index 0: it settles at 1 when outcome 0 won
        agree = None if v2_price is None else (v2_price == (1.0 if winner == 0 else 0.0))
        minutes = max((hi - lo) // 60, 1)
        results.append(
            {
                "condition_id": cid,
                "question": q,
                "event_slug": ev,
                "volume": vol,
                "start": sd,
                "closed": ct or ed,
                "life_days": round((hi - lo) / 86400, 1),
                "clob_1m_points": len(p1),
                "clob_1m_coverage": round(len(p1) / minutes, 3),
                "clob_1m_first_lag_h": round((p1[0][0] - lo) / 3600, 1) if p1 else None,
                "clob_1m_requests": r1,
                "clob_60m_points": len(p60),
                "clob_60m_requests": r60,
                "v2_points": len(v2),
                "v2_resolutions_s": sorted({int(x.get("resolution_seconds") or 0) for x in v2}),
                "trades_fetched": len(trades),
                "trades_complete": complete,
                "trade_pages": rt,
                "trades_earliest": min((t["timestamp"] for t in trades), default=None),
                "trades_usd_fetched": round(sum(t["size"] * t["price"] for t in trades)),
                "uma_status": r.get("status"),
                "was_disputed": r.get("was_disputed"),
                "v2_settled_price_token0": v2_price,
                "gamma_winner_index": winner,
                "resolution_agrees": agree,
            }
        )
        price_rows += [(tok0, cid, t, p, 1) for t, p in p1]
        print(
            f"{q[:60]:60} 1m={len(p1):>6} ({len(p1) / minutes:.0%}) v2={len(v2):>4} "
            f"trades={len(trades):>7}{'' if complete else '+'} res_ok={agree}",
            flush=True,
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"markets": results, "api": api.stats()}, indent=1))
    con.execute("CREATE OR REPLACE TABLE probe AS SELECT * FROM read_json(?)", [str(OUT)])
    con.execute(
        "CREATE OR REPLACE TABLE prices_probe (token_id VARCHAR, condition_id VARCHAR, "
        "ts BIGINT, p DOUBLE, fidelity_min INTEGER)"
    )
    con.executemany("INSERT INTO prices_probe VALUES (?, ?, ?, ?, ?)", price_rows)
    print("api", api.stats())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--max-trade-pages", type=int, default=150)
    a = ap.parse_args()
    run(a.n, a.max_trade_pages)
