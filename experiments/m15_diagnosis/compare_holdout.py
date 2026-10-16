"""Paired comparison of two models' holdout results (e.g. Qwen BD vs Sonnet via Claude Code).

    uv run python experiments/m15_diagnosis/compare_holdout.py \
        --a evals/m15/results/base_holdout.json --a-name "Qwen3-8B BD" \
        --b data/rl/diagnosis/sonnet_cc/sonnet_cc_holdout.json --b-name "Sonnet 5.5 (CC)"

Same tasks only; an unanswered task scores p = 0.5 (as harness.summarize). Differences are
model A minus model B (negative = A better), with 95% CIs from resampling markets (4,000
draws, seed 15). Offline; writes data/rl/diagnosis/compare/<a>_vs_<b>.md.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from gridcase.rl.reward import brier, ece
from gridcase.rl.tasks import load_manifest

ROOT = Path(__file__).resolve().parents[2]


def _rows(path: Path) -> dict[str, dict[str, Any]]:
    return {r["task_id"]: r for r in json.loads(path.read_text())["rows"]}


def _p(r: dict[str, Any]) -> float:
    return 0.5 if r.get("p") is None else float(r["p"])


def _boot(ids: list[str], market: dict[str, str], f: Any) -> tuple[float, float, float]:
    by_m: dict[str, list[str]] = defaultdict(list)
    for t in ids:
        by_m[market[t]].append(t)
    ms = sorted(by_m)
    rng = random.Random(15)
    vals = sorted(f([t for _ in ms for t in by_m[ms[rng.randrange(len(ms))]]]) for _ in range(4000))
    return f(ids), vals[100], vals[3899]


def _corr(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return float(sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (vx * vy) ** 0.5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=Path, required=True)
    ap.add_argument("--b", type=Path, required=True)
    ap.add_argument("--a-name", default="A")
    ap.add_argument("--b-name", default="B")
    ap.add_argument("--manifest", type=Path, default=ROOT / "evals/m15/holdout/manifest.jsonl")
    a = ap.parse_args()
    tasks = {t.task_id: t for t in load_manifest(a.manifest)}
    ra, rb = _rows(a.a), _rows(a.b)
    ids_all = sorted(set(ra) & set(rb))
    market = {t: tasks[t].condition_id for t in ids_all}
    lines = [f"# {a.a_name} vs {a.b_name} (holdout, paired)", ""]
    for kind in ("spike_hold", "resolve"):
        ids = [t for t in ids_all if tasks[t].kind == kind]
        if not ids:
            continue
        y = {t: float(ra[t]["y"]) for t in ids}
        q = {t: float(ra[t]["market_p"]) for t in ids}

        def mean_brier(
            rows: dict[str, dict[str, Any]], sel: list[str], y: dict[str, float] = y
        ) -> float:
            return sum(brier(_p(rows[t]), y[t]) for t in sel) / len(sel)

        def diff(sel: list[str]) -> float:
            return mean_brier(ra, sel) - mean_brier(rb, sel)

        def vs_q(
            rows: dict[str, dict[str, Any]], q: dict[str, float] = q, y: dict[str, float] = y
        ) -> Any:
            return lambda sel: (
                mean_brier(rows, sel) - sum(brier(q[t], y[t]) for t in sel) / len(sel)
            )

        d = _boot(ids, market, diff)
        ga, gb = _boot(ids, market, vs_q(ra)), _boot(ids, market, vs_q(rb))
        lines += [
            f"## {kind} ({len(ids)} tasks on {len({market[t] for t in ids})} markets)",
            "",
            f"| | {a.a_name} | {a.b_name} | market rule q |",
            "|---|---|---|---|",
            f"| Brier | {mean_brier(ra, ids):.4f} | {mean_brier(rb, ids):.4f} | "
            f"{sum(brier(q[t], y[t]) for t in ids) / len(ids):.4f} |",
            f"| Gap to q [95% CI] | {ga[0]:+.4f} [{ga[1]:+.4f}, {ga[2]:+.4f}] | "
            f"{gb[0]:+.4f} [{gb[1]:+.4f}, {gb[2]:+.4f}] | |",
            f"| ECE | {ece([_p(ra[t]) for t in ids], [y[t] for t in ids], bins=10):.3f} | "
            f"{ece([_p(rb[t]) for t in ids], [y[t] for t in ids], bins=10):.3f} | "
            f"{ece([q[t] for t in ids], [y[t] for t in ids], bins=10):.3f} |",
            f"| Unanswered | {sum(ra[t].get('p') is None for t in ids)} | "
            f"{sum(rb[t].get('p') is None for t in ids)} | |",
            f"| Copies q (within 0.01) | "
            f"{sum(abs(_p(ra[t]) - q[t]) <= 0.01 for t in ids) / len(ids):.0%} | "
            f"{sum(abs(_p(rb[t]) - q[t]) <= 0.01 for t in ids) / len(ids):.0%} | |",
        ]
        if kind == "spike_hold":
            down = [float(tasks[t].facts["move_pts"] < 0) for t in ids]
            lines.append(
                f"| Direction corr | {_corr([_p(ra[t]) for t in ids], down):+.2f} | "
                f"{_corr([_p(rb[t]) for t in ids], down):+.2f} | "
                f"{_corr([q[t] for t in ids], down):+.2f} |"
            )
        lines += [
            "",
            f"Paired Brier difference, {a.a_name} − {a.b_name}: "
            f"**{d[0]:+.4f} [{d[1]:+.4f}, {d[2]:+.4f}]** (negative = {a.a_name} better)",
            "",
        ]
    out = ROOT / "data/rl/diagnosis/compare"
    out.mkdir(parents=True, exist_ok=True)
    slug = f"{a.a.stem}_vs_{a.b.stem}"
    (out / f"{slug}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
