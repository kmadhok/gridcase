"""Free check (guide D3): path features for spike_hold tasks, strictly at or before as_of.

Reads data/backfill.duckdb read-only, only through the point-in-time views the tools use:
prices_as_of(as_of), hist_markets_as_of(as_of), trades_as_of(as_of). Never reads hist_outcomes,
closed_at, or anything after as_of. Train, validation and eval tasks only; the holdout is never
loaded.

    uv run --with pandas python experiments/m15_diagnosis/free_path_features.py

Writes data/rl/diagnosis/free/path_features.parquet (one row per task, plus y and split).
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from gridcase import config  # noqa: E402

OUT = ROOT / "data/rl/diagnosis/free"
GRID = timedelta(minutes=10)
LOOKBACK = timedelta(days=30)  # hist.py: a price older than this is not "the price now"


def load(manifest: Path, answers: Path, splits: set[str]) -> list[dict[str, Any]]:
    ys = {}
    for line in answers.open():
        a = json.loads(line)
        ys[a["task_id"]] = a["y"]
    out = []
    for line in manifest.open():
        t = json.loads(line)
        if t["kind"] == "spike_hold" and t["split"] in splits:
            t["y"] = ys[t["task_id"]]
            out.append(t)
    return out


def grid_series(pts: list[tuple[datetime, float]], as_of: datetime, hours: int) -> list[float]:
    """Last price carried forward on the 10-minute grid ending at as_of (as tasks.detect_spikes)."""
    n = int(timedelta(hours=hours) / GRID)
    out: list[float] = []
    j = 0
    last: float | None = None
    for k in range(n, -1, -1):
        t = as_of - k * GRID
        while j < len(pts) and pts[j][0] <= t:
            last = pts[j][1]
            j += 1
        out.append(math.nan if last is None else last)
    return out


def sd(xs: list[float]) -> float:
    xs = [x for x in xs if not math.isnan(x)]
    return statistics.pstdev(xs) if len(xs) > 1 else math.nan


def features(cur: duckdb.DuckDBPyConnection, t: dict[str, Any]) -> dict[str, Any]:
    as_of = datetime.fromisoformat(t["as_of"])
    cid = t["condition_id"]
    f = t["facts"]
    pb, pn, move = float(f["price_before"]), float(f["price_now"]), float(f["move_pts"]) / 100
    s = 1.0 if move > 0 else -1.0
    q = pn if s > 0 else 1 - pn
    row: dict[str, Any] = {
        "task_id": t["task_id"],
        "split": t["split"],
        "cluster_id": t["cluster_id"],
        "category": t["category"],
        "y": t["y"],
        "q": q,
        "up": int(s > 0),
        "abs_move": abs(move),
        "price_before": pb,
        "price_now": pn,
    }

    # --- price path (prices_as_of) ---
    pts = cur.execute(
        "SELECT ts, p FROM prices_as_of(?) WHERE condition_id = ? AND ts > ? ORDER BY ts",
        [as_of, cid, as_of - timedelta(days=7) - LOOKBACK],
    ).fetchall()
    pts = [(a, float(b)) for a, b in pts]
    g7 = grid_series(pts, as_of, 24 * 7)  # 1009 points, g7[-1] = price at as_of
    d10 = [b - a for a, b in zip(g7, g7[1:], strict=False)]
    h7 = g7[::6]  # hourly grid ending at as_of (since len-1 = 1008 is a multiple of 6)
    d1h = [b - a for a, b in zip(h7, h7[1:], strict=False)]
    pre = 36  # 6h of 10-min steps
    row["check_pb_grid"], row["check_pn_grid"] = g7[-37], g7[-1]  # should equal the facts
    row["vol10m_24h"] = sd(d10[-144:])
    row["vol10m_7d"] = sd(d10)
    row["vol1h_24h"] = sd(d1h[-24:])
    row["vol1h_7d"] = sd(d1h)
    # background volatility, excluding the move window
    row["vol10m_7d_pre"] = sd(d10[:-pre])
    row["vol1h_7d_pre"] = sd(d1h[:-6])
    row["move_z"] = (
        abs(move) / (row["vol1h_7d_pre"] * math.sqrt(6))
        if row["vol1h_7d_pre"] and not math.isnan(row["vol1h_7d_pre"])
        else math.nan
    )
    win = d10[-pre:]
    signed = [s * x for x in win if not math.isnan(x)]
    tot = sum(signed)
    p1h = g7[-7]
    row["share_last1h"] = s * (pn - p1h) / abs(move) if not math.isnan(p1h) else math.nan
    pos = sorted((x for x in signed if x > 0), reverse=True)
    acc, k = 0.0, 0
    for x in pos:
        acc += x
        k += 1
        if acc >= 0.8 * abs(move) - 1e-9:
            break
    row["n_steps80"] = k if acc >= 0.8 * abs(move) - 1e-9 else 36
    row["max_step_share"] = (pos[0] / abs(move)) if pos else 0.0
    row["single_jump"] = int(bool(pos) and pos[0] >= 0.8 * abs(move) - 1e-9)
    row["path_efficiency"] = tot / sum(abs(x) for x in signed) if signed and any(signed) else 0.0
    row["n_points_6h"] = sum(1 for a, _ in pts if a > as_of - timedelta(hours=6))
    row["n_points_7d"] = sum(1 for a, _ in pts if a > as_of - timedelta(days=7))
    row["minutes_since_last_point"] = (as_of - pts[-1][0]).total_seconds() / 60 if pts else math.nan
    # range over 7d (price excursion before the move)
    pre_vals = [x for x in g7[:-pre] if not math.isnan(x)]
    row["range_7d_pre"] = (max(pre_vals) - min(pre_vals)) if pre_vals else math.nan
    row["flat_before"] = int(bool(pre_vals) and (max(pre_vals) - min(pre_vals)) < 0.005)

    # --- market metadata (hist_markets_as_of) ---
    m = cur.execute(
        "SELECT start_at, end_at, event_slug, neg_risk, outcome0 FROM hist_markets_as_of(?) "
        "WHERE condition_id = ?",
        [as_of, cid],
    ).fetchall()
    start, end, ev, neg, o0 = m[0] if m else (None, None, None, None, None)
    row["dist50_before"] = abs(pb - 0.5)
    row["before_at_50"] = int(abs(pb - 0.5) < 0.0051)
    row["age_hours"] = (as_of - start).total_seconds() / 3600 if start else math.nan
    row["young12h"] = int(start is not None and as_of - start < timedelta(hours=12))
    row["hours_to_end"] = (end - as_of).total_seconds() / 3600 if end else math.nan
    row["neg_risk"] = int(bool(neg))

    # --- siblings (hist_markets_as_of + prices_as_of), as hist.siblings over 6h ---
    sib = (
        cur.execute(
            """
        WITH ids AS (SELECT condition_id FROM hist_markets_as_of(?)
                     WHERE event_slug = ? AND condition_id <> ?),
        now_p AS (SELECT condition_id, arg_max(p, ts) AS p FROM prices_as_of(?)
                  WHERE condition_id IN (SELECT condition_id FROM ids) AND ts > ?
                  GROUP BY condition_id),
        then_p AS (SELECT condition_id, arg_max(p, ts) AS p FROM prices_as_of(?)
                   WHERE condition_id IN (SELECT condition_id FROM ids) AND ts <= ? AND ts > ?
                   GROUP BY condition_id)
        SELECT ids.condition_id, now_p.p, then_p.p
        FROM ids LEFT JOIN now_p USING (condition_id) LEFT JOIN then_p USING (condition_id)
        """,
            [
                as_of,
                ev,
                cid,
                as_of,
                as_of - LOOKBACK,
                as_of,
                as_of - timedelta(hours=6),
                as_of - timedelta(hours=6) - LOOKBACK,
            ],
        ).fetchall()
        if ev
        else []
    )
    ch = [a - b for _, a, b in sib if a is not None and b is not None]
    row["n_siblings"] = len(sib)
    row["sib_same"] = sum(1 for c in ch if s * c >= 0.02)
    row["sib_opp"] = sum(1 for c in ch if s * c <= -0.02)
    row["sib_same_share"] = row["sib_same"] / len(ch) if ch else 0.0
    row["sib_opp_share"] = row["sib_opp"] / len(ch) if ch else 0.0

    # --- flow (trades_as_of), last 6h vs the 6h before ---
    st = cur.execute("SELECT complete FROM trades_status WHERE condition_id = ?", [cid]).fetchall()
    row["flow_ok"] = int(bool(st) and bool(st[0][0]))
    if row["flow_ok"]:
        tr = cur.execute(
            """
            SELECT ts > ? AS recent, outcome = ? AS o0, side = 'BUY' AS buy,
                   count(*), sum(usd), count(DISTINCT wallet)
            FROM trades_as_of(?) WHERE condition_id = ? AND ts > ?
            GROUP BY ALL
            """,
            [as_of - timedelta(hours=6), o0, as_of, cid, as_of - timedelta(hours=12)],
        ).fetchall()
        agg = {"n": [0, 0], "usd": [0.0, 0.0], "with": [0.0, 0.0], "buy": [0.0, 0.0]}
        for recent, is0, buy, n, usd, _w in tr:
            i = 1 if recent else 0
            usd = float(usd or 0.0)
            agg["n"][i] += n
            agg["usd"][i] += usd
            up_pressure = bool(is0) == bool(buy)  # buy outcome0 or sell outcome1
            if (s > 0) == up_pressure:
                agg["with"][i] += usd
            if buy:
                agg["buy"][i] += usd
        row["trades_6h"] = agg["n"][1]
        row["log_trades_6h"] = math.log1p(agg["n"][1])
        row["log_usd_6h"] = math.log1p(agg["usd"][1])
        row["log_trade_ratio"] = math.log1p(agg["n"][1]) - math.log1p(agg["n"][0])
        row["log_usd_ratio"] = math.log1p(agg["usd"][1]) - math.log1p(agg["usd"][0])
        row["with_move_share"] = agg["with"][1] / agg["usd"][1] if agg["usd"][1] else math.nan
        row["buy_share_6h"] = agg["buy"][1] / agg["usd"][1] if agg["usd"][1] else math.nan
    return row


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tasks = load(
        ROOT / "evals/m15/train_manifest.jsonl", ROOT / "data/rl/train_answers.jsonl", {"train"}
    ) + load(
        ROOT / "evals/m15/manifest.jsonl", ROOT / "data/rl/answers.jsonl", {"eval", "validation"}
    )
    print(f"{len(tasks)} spike tasks", flush=True)
    con = duckdb.connect(str(config.BACKFILL_DB), read_only=True)
    t0 = time.time()

    def work(chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cur = con.cursor()
        return [features(cur, t) for t in chunk]

    if len(sys.argv) > 1:
        tasks = tasks[: int(sys.argv[1])]
    chunks = [tasks[i::4] for i in range(4)]
    with ThreadPoolExecutor(4) as ex:
        rows = [r for part in ex.map(work, chunks) for r in part]
    print(f"features in {time.time() - t0:.0f}s", flush=True)
    pd.DataFrame(rows).to_parquet(OUT / "path_features.parquet", index=False)


if __name__ == "__main__":
    main()
