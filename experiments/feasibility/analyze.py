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
import random
from collections import defaultdict
from datetime import UTC, datetime
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


def load(
    con: duckdb.DuckDBPyConnection, markets_t: str, prices_t: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Series]]:
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
        for r in con.execute(f"SELECT {', '.join(cols)} FROM {markets_t}").fetchall()
    }
    pts: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for cid, t, p in con.execute(
        f"SELECT condition_id, ts, p FROM {prices_t} ORDER BY condition_id, ts"
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


def clusters(
    sp: list[dict[str, Any]], generic: set[str]
) -> tuple[int, int, dict[int, int], dict[int, int]]:
    """(event clusters, news clusters, spike -> news cluster id, spike -> event cluster id)."""
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
    event_ids = {i: find(i) for i in range(len(sp))}
    n_event = len(set(event_ids.values()))
    for a, i in enumerate(order):  # shared specific tag within 2h
        ti = set(sp[i]["tags"].split("|")) - generic - {""}
        for j in order[a + 1 :]:
            if sp[j]["t"] - sp[i]["t"] > 2 * 3600:
                break
            if sp[j]["ev"] != sp[i]["ev"] and ti & (set(sp[j]["tags"].split("|")) - generic):
                join(i, j)
    ids = {i: find(i) for i in range(len(sp))}
    return n_event, len(set(ids.values())), ids, event_ids


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


def generic_tags(
    con: duckdb.DuckDBPyConnection | None, markets: dict[str, dict[str, Any]]
) -> set[str]:
    """Tags too broad to say two events share news: on > 2% of the frame's events (census), or
    on > 10% of the sampled events when there is no census (the collector run)."""
    if con is not None:
        rows = con.execute(
            "WITH e AS (SELECT ev, any_value(tags) AS tags FROM frame GROUP BY ev), "
            "t AS (SELECT DISTINCT ev, unnest(string_split(tags, '|')) AS tag FROM e) "
            "SELECT tag FROM t WHERE tag <> '' GROUP BY tag "
            "HAVING count(*) > 0.02 * (SELECT count(*) FROM e)"
        ).fetchall()
        return {r[0] for r in rows}
    n_ev: dict[str, int] = defaultdict(int)
    for tags in {m["ev"]: m["tags"] for m in markets.values()}.values():
        for tag in set((tags or "").split("|")) - {""}:
            n_ev[tag] += 1
    n_events = len({m["ev"] for m in markets.values()})
    return {t for t, n in n_ev.items() if n > 0.10 * n_events}


def bootstrap(
    per_event: dict[str, float], events: dict[str, tuple[str, float]], reps: int = 300
) -> tuple[float, float]:
    """90% interval of sum(weight x per-event count), resampling events within strata."""
    rng = random.Random(1)
    by_stratum: dict[str, list[str]] = defaultdict(list)
    for ev, (stratum, _) in events.items():
        by_stratum[stratum].append(ev)
    totals = []
    for _ in range(reps):
        tot = 0.0
        for evs in by_stratum.values():
            for ev in rng.choices(evs, k=len(evs)):
                tot += events[ev][1] * per_event.get(ev, 0.0)
        totals.append(tot)
    totals.sort()
    return totals[int(0.05 * reps)], totals[int(0.95 * reps)]


def main(no_census: bool, prefix: str, window: tuple[int, int] | None) -> None:
    con = duckdb.connect(str(DB))
    markets_t, prices_t = (
        (f"{prefix}_markets", f"{prefix}_prices") if prefix else ("sample_markets", "prices")
    )
    markets, series = load(con, markets_t, prices_t)
    report: dict[str, Any] = {"db": str(DB), "markets_table": markets_t, "window": window}
    if not no_census:
        report["liquidity"] = liquidity(con)
    generic = generic_tags(None if no_census else con, markets)
    events = {m["ev"]: (m["stratum"], float(m["weight"])) for m in markets.values()}
    report["generic_tags"] = sorted(generic)
    report["sample"] = {
        "events": len(events),
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
            if window is None or window[0] <= e["t"] < window[1]
        ]
        held24 = sum(1 for e in sp if e["r24"] is not None and e["r24"] >= 0.5)
        held72 = sum(1 for e in sp if e["r72"] is not None and e["r72"] >= 0.5)
        clean72 = sum(
            1 for e in sp if e["r72"] is not None and (e["r72"] >= 0.75 or e["r72"] <= 0.25)
        )
        n_event, n_news, ids, ev_ids = clusters(sp, generic) if sp else (0, 0, {}, {})
        w_spikes = sum(e["weight"] for e in sp)
        # weight each news cluster once, by its first spike's stratum weight
        first: dict[int, float] = {}
        for i, e in enumerate(sp):
            first.setdefault(ids[i], e["weight"])
        # event clusters never span events, so they decompose per event: bootstrap those
        ev_clusters: dict[str, set[int]] = defaultdict(set)
        for i, e in enumerate(sp):
            ev_clusters[e["ev"]].add(ev_ids[i])
        per_event = {ev: float(len(c)) for ev, c in ev_clusters.items()}
        lo, hi = bootstrap(per_event, events) if sp else (0.0, 0.0)
        w_event_clusters = sum(events[ev][1] * n for ev, n in per_event.items())
        if x == 10 and y == 6 and sp:
            report.setdefault("examples", [])
            rng = random.Random(x * 100 + int(vmin))
            for e in rng.sample(sp, min(8, len(sp))):
                report["examples"].append(
                    {
                        "min_volume": vmin,
                        "question": markets[e["cid"]]["question"],
                        "t": datetime.fromtimestamp(e["t"], UTC).strftime("%Y-%m-%d %H:%M"),
                        "base": round(e["base"], 3),
                        "move": round(e["move"], 3),
                        "r24": None if e["r24"] is None else round(e["r24"], 2),
                        "r72": None if e["r72"] is None else round(e["r72"], 2),
                    }
                )
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
                "est_event_clusters_6mo": round(w_event_clusters),
                "est_event_clusters_90ci": f"{round(lo)}-{round(hi)}",
                "est_news_clusters_6mo": round(sum(first.values())),
            }
        )
    report["sensitivity"] = rows

    # 5. Brier-usable resolved markets
    brier: dict[str, float] = defaultdict(float)
    brier_events: dict[str, set[str]] = defaultdict(set)
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
                if m["ev"] not in brier_events[label]:
                    brier_events[label].add(m["ev"])
                    brier[f"events_{label}"] += 1
                    brier[f"est_events_{label}_6mo"] += m["weight"]
                if m["volume"] >= 1e5:
                    brier[f"markets_{label}_ge_100k"] += 1
                    brier[f"est_markets_{label}_ge_100k_6mo"] += m["weight"]
    report["brier"] = {k: round(v) for k, v in sorted(brier.items())}
    out = OUT.with_name(OUT.stem + (f"_{prefix}" if prefix else "") + ".json")
    out.write_text(json.dumps(report, indent=1, default=str))
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
    ap.add_argument(
        "--prefix", default="", help="read <prefix>_markets / <prefix>_prices (e.g. dense)"
    )
    ap.add_argument(
        "--window", nargs=2, metavar=("START", "END"), help="only spikes in [START, END)"
    )
    a = ap.parse_args()
    win = None
    if a.window:
        win = tuple(
            int(datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp()) for d in a.window
        )
    main(a.no_census, a.prefix, win)  # type: ignore[arg-type]
