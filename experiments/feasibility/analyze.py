# ruff: noqa: E501  (SQL and table rows read better unwrapped)
"""Phase 3: count gradable events in the sampled price series.

Reads `prices` + `sample_markets` (sample.py) and `census` from backfill.duckdb and prints:
  1. liquidity: the volume distribution of the frame (to pick thresholds from the data);
  2. a spike sensitivity table over (X points, Y hours, minimum market volume);
  3. held vs reverted at +24h and +72h for each cell;
  4. clustering: spikes -> event clusters -> news clusters;
  5. Brier-usable resolved markets;
  6. extrapolation to the census (6 months) using the stratum weights.
Writes every table to data/feasibility/phase3.json.

Definitions (all on token 0, the outcome-index-0 price, on a 10-minute grid, last price carried
forward; CLOB's series is one point per fidelity bucket):
  spike at t:    |p(t) - p(t - Y)| >= X/100, with p(t - Y) in [0.05, 0.95] (not already decided),
                 t - Y >= first point and t + 72h <= the market's close (labels never see the
                 resolution). After a spike the market is skipped for max(Y, 24h), so a move
                 and its own reversal count once (at most one task per market per day).
  retained(H):   (p(t + H) - p(t - Y)) / (p(t) - p(t - Y))
  held / reverted at H: retained >= 0.5 / < 0.5.  "clean" = retained >= 0.75 or <= 0.25.
  event cluster: spikes in markets of the same event within 6 hours of each other.
  news cluster:  event clusters joined when two spikes are within 2 hours and their events share
                 a specific tag (tags on more than 10% of sampled events count as generic).
  Brier-usable:  resolved binary market whose price 24h (and 7d) before close is in [0.05, 0.95].

    uv run python experiments/feasibility/analyze.py
"""

from __future__ import annotations

import bisect
import json
from collections import defaultdict
from itertools import product
from typing import Any

import duckdb
from census import DB
from probe import life

GRID_S = 600
COOLDOWN_S = 24 * 3600
XS = (5, 10, 15, 20, 30)
YS = (1, 3, 6, 24)
VOL_MINS = (1e4, 1e5, 1e6)
HORIZONS_H = (24, 72)
OUT = DB.with_name(DB.stem + "_phase3.json")


class Series:
    def __init__(self, pts: list[tuple[int, float]]) -> None:
        self.t = [a for a, _ in pts]
        self.p = [b for _, b in pts]

    def at(self, ts: float) -> float | None:  # last price at or before ts
        i = bisect.bisect_right(self.t, ts) - 1
        return self.p[i] if i >= 0 else None


def load(con: duckdb.DuckDBPyConnection) -> tuple[dict[str, dict[str, Any]], dict[str, Series]]:
    cols = [
        "condition_id",
        "ev",
        "question",
        "volume",
        "start_date",
        "end_date",
        "closed_time",
        "tags",
        "stratum",
        "weight",
        "winner_index",
        "ev_volume",
    ]
    markets = {
        r[0]: dict(zip(cols, r, strict=True))
        for r in con.execute(f"SELECT {', '.join(cols)} FROM sample_markets").fetchall()
    }
    pts: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for cid, t, p in con.execute(
        "SELECT condition_id, ts, p FROM prices ORDER BY condition_id, ts"
    ).fetchall():
        pts[cid].append((t, p))
    return markets, {c: Series(v) for c, v in pts.items()}


def spikes(m: dict[str, Any], s: Series, x: float, y_h: int) -> list[dict[str, Any]]:
    if not s.t:
        return []
    _, close = life(m["start_date"], m["end_date"], m["closed_time"])
    y = y_h * 3600
    t = (s.t[0] // GRID_S + 1) * GRID_S + y
    last = min(close, s.t[-1]) - 72 * 3600
    out = []
    while t <= last:
        base, now = s.at(t - y), s.at(t)
        if (
            base is not None
            and now is not None
            and 0.05 <= base <= 0.95
            and abs(now - base) >= x / 100
        ):
            move = now - base
            lab = {}
            for h in HORIZONS_H:
                later = s.at(t + h * 3600)
                lab[h] = None if later is None else (later - base) / move
            out.append(
                {
                    "cid": m["condition_id"],
                    "ev": m["ev"],
                    "t": t,
                    "base": base,
                    "move": move,
                    "r24": lab[24],
                    "r72": lab[72],
                    "weight": m["weight"],
                    "stratum": m["stratum"],
                    "tags": m["tags"] or "",
                }
            )
            t += max(y, COOLDOWN_S)
        else:
            t += GRID_S
    return out


def clusters(sp: list[dict[str, Any]], generic: set[str]) -> tuple[int, int, dict[int, int]]:
    """(event clusters, news clusters, spike index -> news cluster id)."""
    parent = list(range(len(sp)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    order = sorted(range(len(sp)), key=lambda i: sp[i]["t"])
    for a, i in enumerate(order):  # same event within 6h
        for j in order[a + 1 :]:
            if sp[j]["t"] - sp[i]["t"] > 6 * 3600:
                break
            if sp[j]["ev"] == sp[i]["ev"]:
                join(i, j)
    n_event = len({find(i) for i in range(len(sp))})
    for a, i in enumerate(order):  # shared specific tag within 2h
        ti = set(sp[i]["tags"].split("|")) - generic - {""}
        for j in order[a + 1 :]:
            if sp[j]["t"] - sp[i]["t"] > 2 * 3600:
                break
            if sp[j]["ev"] != sp[i]["ev"] and ti & (set(sp[j]["tags"].split("|")) - generic):
                join(i, j)
    ids = {i: find(i) for i in range(len(sp))}
    return n_event, len(set(ids.values())), ids


def liquidity(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Volume distribution of the frame (resolved binary, non-recurring, >= $1k)."""
    q = con.execute(
        "SELECT quantile_cont(volume, [0.25, 0.5, 0.75, 0.9, 0.99]) FROM frame"
    ).fetchone()
    tiers = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE volume >= 1e4), count(*) FILTER (WHERE volume >= 1e5), "
        "count(*) FILTER (WHERE volume >= 1e6), count(DISTINCT ev), count(DISTINCT ev) FILTER (WHERE volume >= 1e5) "
        "FROM frame"
    ).fetchone()
    census_tot = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE recurring), count(*) FILTER (WHERE winner_index IS NULL) FROM census"
    ).fetchone()
    return {
        "census_markets": census_tot[0],
        "census_recurring": census_tot[1],
        "census_no_binary_winner": census_tot[2],
        "frame_volume_quantiles_25_50_75_90_99": [round(v) for v in q[0]],
        "frame_markets": tiers[0],
        "frame_ge_10k": tiers[1],
        "frame_ge_100k": tiers[2],
        "frame_ge_1M": tiers[3],
        "frame_events": tiers[4],
        "frame_events_with_market_ge_100k": tiers[5],
    }


def main(no_census: bool) -> None:
    con = duckdb.connect(str(DB))
    markets, series = load(con)
    report: dict[str, Any] = {"db": str(DB)}
    if not no_census:
        report["liquidity"] = liquidity(con)
    n_ev = defaultdict(int)
    for m in markets.values():
        for tag in set((m["tags"] or "").split("|")) - {""}:
            n_ev[tag] += 1
    n_events = len({m["ev"] for m in markets.values()})
    generic = {t for t, n in n_ev.items() if n > 0.10 * n_events}
    report["generic_tags"] = sorted(generic)
    report["sample"] = {
        "events": n_events,
        "markets": len(markets),
        "markets_with_prices": sum(1 for c in markets if series.get(c) and series[c].t),
        "price_points": sum(len(s.t) for s in series.values()),
    }

    # 2-4. sensitivity table
    rows = []
    for x, y, vmin in product(XS, YS, VOL_MINS):
        sp = [
            e
            for m in markets.values()
            if m["volume"] >= vmin
            for e in spikes(m, series.get(m["condition_id"]) or Series([]), x, y)
        ]
        held24 = sum(1 for e in sp if e["r24"] is not None and e["r24"] >= 0.5)
        held72 = sum(1 for e in sp if e["r72"] is not None and e["r72"] >= 0.5)
        clean72 = sum(
            1 for e in sp if e["r72"] is not None and (e["r72"] >= 0.75 or e["r72"] <= 0.25)
        )
        n_event, n_news, ids = clusters(sp, generic) if sp else (0, 0, {})
        w_spikes = sum(e["weight"] for e in sp)
        # weight each news cluster once, by its first spike's stratum weight
        first: dict[int, float] = {}
        for i, e in enumerate(sp):
            first.setdefault(ids[i], e["weight"])
        rows.append(
            {
                "x_pts": x,
                "y_h": y,
                "min_volume": vmin,
                "spikes": len(sp),
                "held24": held24,
                "held72": held72,
                "reverted72": len(sp) - held72,
                "held72_share": round(held72 / len(sp), 2) if sp else None,
                "clean72_share": round(clean72 / len(sp), 2) if sp else None,
                "event_clusters": n_event,
                "news_clusters": n_news,
                "est_spikes_6mo": round(w_spikes),
                "est_news_clusters_6mo": round(sum(first.values())),
            }
        )
    report["sensitivity"] = rows

    # 5. Brier-usable resolved markets
    brier = defaultdict(float)
    for m in markets.values():
        s = series.get(m["condition_id"])
        if not s or not s.t:
            continue
        _, close = life(m["start_date"], m["end_date"], m["closed_time"])
        for label, h in (("24h", 24), ("7d", 168)):
            p = s.at(close - h * 3600) if close - h * 3600 >= s.t[0] else None
            if p is not None and 0.05 <= p <= 0.95:
                brier[f"markets_{label}"] += 1
                brier[f"est_markets_{label}_6mo"] += m["weight"]
                if m["volume"] >= 1e5:
                    brier[f"markets_{label}_ge_100k"] += 1
                    brier[f"est_markets_{label}_ge_100k_6mo"] += m["weight"]
    report["brier"] = {k: round(v) for k, v in sorted(brier.items())}
    OUT.write_text(json.dumps(report, indent=1, default=str))
    print(
        json.dumps({k: v for k, v in report.items() if k != "sensitivity"}, indent=1, default=str)
    )
    hdr = list(rows[0])
    print("\n| " + " | ".join(hdr) + " |\n|" + "---|" * len(hdr))
    for r in rows:
        print("| " + " | ".join(str(r[h]) for h in hdr) + " |")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-census", action="store_true", help="collector.duckdb has no census")
    main(ap.parse_args().no_census)
