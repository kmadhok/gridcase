"""M15 holdout grading, exactly as pre-registered in docs/DECISIONS.md (2026-10-16).

    uv run python experiments/m15_diagnosis/holdout_grade.py \
        --results evals/m15/results/base_holdout.json --manifest evals/m15/holdout/manifest.jsonl

Committed before the holdout run. Spike tasks: an unanswered (malformed) task is scored at
p = 0.5, and answered-only figures are reported alongside. q is the market-implied rule
(market_p in the results rows). The bootstrap resamples markets (condition_id), 4,000 draws,
seed 15. Writes <out>/grade.json and grade.md.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from gridcase.rl.reward import brier, ece
from gridcase.rl.tasks import load_manifest

DRAWS, SEED = 4000, 15


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _corr(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return cov / math.sqrt(vx * vy) if vx > 1e-12 and vy > 1e-12 else float("nan")


def _boot(rows: list[dict[str, Any]], stat: Any) -> tuple[float, float, float]:
    """stat(rows) with a 95% CI from resampling markets."""
    by_m: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_m[r["market"]].append(r)
    ms = sorted(by_m)
    rng = random.Random(SEED)
    vals = []
    for _ in range(DRAWS):
        pick = [r for _ in ms for r in by_m[ms[rng.randrange(len(ms))]]]
        vals.append(stat(pick))
    vals.sort()
    return stat(rows), vals[int(0.025 * DRAWS)], vals[int(0.975 * DRAWS) - 1]


def grade(results: Path, manifest: Path) -> dict[str, Any]:
    tasks = {t.task_id: t for t in load_manifest(manifest)}
    res = json.loads(results.read_text())
    spike: list[dict[str, Any]] = []
    resolve: list[dict[str, Any]] = []
    for r in res["rows"]:
        t = tasks[r["task_id"]]
        row: dict[str, Any] = {
            "task_id": t.task_id, "market": t.condition_id, "y": float(r["y"]),
            "q": float(r["market_p"]), "answered": r.get("p") is not None,
            "p": float(r["p"]) if r.get("p") is not None else 0.5,
        }  # fmt: skip
        if t.kind == "spike_hold":
            down = float(t.facts["move_pts"]) < 0
            pn = float(t.facts["price_now"])
            row |= {"down": down, "headroom": pn if down else 1 - pn}
            spike.append(row)
        else:
            resolve.append(row)

    def gap(rs: list[dict[str, Any]]) -> float:
        return _mean([brier(r["p"], r["y"]) - brier(r["q"], r["y"]) for r in rs])

    def blend_gap(rs: list[dict[str, Any]]) -> float:
        return _mean(
            [brier(0.25 * r["p"] + 0.75 * r["q"], r["y"]) - brier(r["q"], r["y"]) for r in rs]
        )

    def dir_corr(rs: list[dict[str, Any]]) -> float:
        return _corr([r["p"] for r in rs], [float(r["down"]) for r in rs])

    answered = [r for r in spike if r["answered"]]
    out: dict[str, Any] = {
        "results_file": str(results),
        "n_spike": len(spike),
        "n_markets": len({r["market"] for r in spike}),
        "malformed_spike": 1 - len(answered) / len(spike),
        "brier_model": _mean([brier(r["p"], r["y"]) for r in spike]),
        "brier_q": _mean([brier(r["q"], r["y"]) for r in spike]),
        "primary_gap": _boot(spike, gap),
        "primary_gap_answered_only": _boot(answered, gap),
        "dir_corr": _boot(spike, dir_corr),
        "sd_p": math.sqrt(_mean([(r["p"] - _mean([s["p"] for s in spike])) ** 2 for r in spike])),
        "ece_model": ece([r["p"] for r in spike], [r["y"] for r in spike], bins=10),
        "ece_q": ece([r["q"] for r in spike], [r["y"] for r in spike], bins=10),
        "copy_q_share": _mean([float(abs(r["p"] - r["q"]) <= 0.01) for r in spike]),
        "stretch_blend_gap": _boot(spike, blend_gap),
    }
    for name, flag in (("up", False), ("down", True)):
        sel = [r for r in spike if r["down"] == flag]
        out[name] = {
            "n": len(sel),
            "mean_p": _mean([r["p"] for r in sel]),
            "hold_rate": _mean([r["y"] for r in sel]),
            "corr_p_headroom": _corr([r["p"] for r in sel], [r["headroom"] for r in sel]),
            "brier": _mean([brier(r["p"], r["y"]) for r in sel]),
            "brier_q": _mean([brier(r["q"], r["y"]) for r in sel]),
        }
    out["resolve"] = {
        "n": len(resolve),
        "malformed": 1 - _mean([float(r["answered"]) for r in resolve]) if resolve else None,
        "brier_model": _mean([brier(r["p"], r["y"]) for r in resolve]),
        "brier_price": _mean([brier(r["q"], r["y"]) for r in resolve]),
    }
    checks = {
        "PRIMARY non-inferior to q (95% upper bound <= +0.015)": out["primary_gap"][2] <= 0.015,
        "direction corr > 0, CI excludes 0": out["dir_corr"][1] > 0,
        "up mean p within 0.07 of hold rate": abs(out["up"]["mean_p"] - out["up"]["hold_rate"])
        <= 0.07,
        "down mean p within 0.07 of hold rate": abs(
            out["down"]["mean_p"] - out["down"]["hold_rate"]
        )
        <= 0.07,
        "corr(p, headroom) < 0 for rises": out["up"]["corr_p_headroom"] < 0,
        "corr(p, headroom) < 0 for drops": out["down"]["corr_p_headroom"] < 0,
        "sd(p) >= 0.12": out["sd_p"] >= 0.12,
        "ECE <= ECE(q) + 0.01": out["ece_model"] <= out["ece_q"] + 0.01,
        "malformed <= 5%": out["malformed_spike"] <= 0.05,
        "STRETCH blend beats q (95% upper bound < 0)": out["stretch_blend_gap"][2] < 0,
    }
    out["checks"] = checks
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("data/rl/diagnosis/holdout"))
    a = ap.parse_args()
    g = grade(a.results, a.manifest)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "grade.json").write_text(json.dumps(g, indent=1))

    def ci(t: tuple[float, float, float], d: int = 4) -> str:
        return f"{t[0]:+.{d}f} [{t[1]:+.{d}f}, {t[2]:+.{d}f}]"

    lines = [
        f"# M15 holdout grade ({a.results})",
        "",
        f"Spike tasks {g['n_spike']} on {g['n_markets']} markets; malformed "
        f"{g['malformed_spike']:.1%}. Brier model {g['brier_model']:.4f}, q {g['brier_q']:.4f}.",
        "",
        f"- Primary gap (model − q, unanswered at 0.5): {ci(g['primary_gap'])}",
        f"- Answered only: {ci(g['primary_gap_answered_only'])}",
        f"- Direction corr: {ci(g['dir_corr'], 2)}",
        f"- Rises: mean p {g['up']['mean_p']:.3f} vs hold {g['up']['hold_rate']:.3f}; "
        f"corr(p, headroom) {g['up']['corr_p_headroom']:+.2f}",
        f"- Drops: mean p {g['down']['mean_p']:.3f} vs hold {g['down']['hold_rate']:.3f}; "
        f"corr(p, headroom) {g['down']['corr_p_headroom']:+.2f}",
        f"- sd(p) {g['sd_p']:.3f}; ECE {g['ece_model']:.3f} vs q {g['ece_q']:.3f}; "
        f"copy-q share {g['copy_q_share']:.2f}",
        f"- Stretch, 0.25·p + 0.75·q vs q: {ci(g['stretch_blend_gap'])}",
        f"- Resolve ({g['resolve']['n']}): Brier {g['resolve']['brier_model']:.4f} vs price "
        f"{g['resolve']['brier_price']:.4f}",
        "",
        "| check | result |",
        "|---|---|",
        *[f"| {k} | {'PASS' if v else 'FAIL'} |" for k, v in g["checks"].items()],
    ]
    (a.out / "grade.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
