"""M15 P3 analysis: the BD framing with tools on the validation spike tasks.

    uv run python experiments/m15_diagnosis/p3_analyze.py

Reads data/rl/diagnosis/probes/p3_*.json(l) and writes data/rl/diagnosis/p3/summary.json and
summary.md: per arm the gates of docs/M15_FINETUNING_GUIDE.md §5 P3 (spike Brier, gap to the
market-implied q, direction corr, down corr(p, q), within-direction corr(p, y) and
corr(p, headroom), sd(p), copy-q share, malformed / over budget / answered-as-text, verifier
drops, frame mismatches), k-sample averaging, per-direction recalibration (cross-fitted by
cluster) and the 0.25·p + 0.75·q blend against q, with cluster-bootstrap CIs. Offline.
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from gridcase.rl.cli import _load
from gridcase.rl.harness import market_baseline
from gridcase.rl.tasks import Task

ROOT = Path(__file__).resolve().parents[2]
PROBES = ROOT / "data" / "rl" / "diagnosis" / "probes"
OUT = ROOT / "data" / "rl" / "diagnosis" / "p3"
ARMS = {
    "bd_off": ["p3_bd_off"],
    "fewshot_off": ["p3_fewshot_off"],
    "bd_on": ["p3_bd_on_s1"],
    "bd_on_avg3": ["p3_bd_on_s1", "p3_bd_on_s2", "p3_bd_on_s3"],
}
REFERENCE = {"a_off (framing A, capture run)": ["capture_off"]}


def _corr(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return cov / math.sqrt(vx * vy) if vx > 1e-12 and vy > 1e-12 else float("nan")


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _sd(xs: list[float]) -> float:
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs)) if xs else float("nan")


def load_run(name: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    res = json.loads((PROBES / f"{name}.json").read_text())
    rows = {r["task_id"]: r for r in res["rows"]}
    eps = {}
    path = PROBES / f"{name}_episodes.jsonl"
    if path.exists():
        for line in path.open():
            e = json.loads(line)
            eps[e["task_id"]] = e
    return rows, eps


def episode_flags(e: dict[str, Any], task: Task) -> dict[str, bool]:
    """Answered as text; a frame mismatch (the prompt's price_now differs from the own-market
    hist.move price the model saw)."""
    tr = e["transcript"]
    submit = next((s for s in tr if s.get("role") == "assistant" and "submit" in s), None)
    as_text = bool(submit and '"probability"' in (submit.get("text") or ""))
    prompt = tr[0]["content"]
    shown_now = None
    if task.kind == "spike_hold":
        try:
            shown_now = float(prompt.split(" to ")[1].split(" (")[0])
        except (IndexError, ValueError):
            shown_now = None
    mismatch = False
    for s in tr:
        if s.get("role") != "tool":
            continue
        for (name, args), (_, content) in zip(s["calls"], s["results"], strict=True):
            if name != "hist_move" or args.get("condition_id") != task.condition_id:
                continue
            try:
                out = json.loads(content).get("output", {})
            except json.JSONDecodeError:
                continue
            price = out.get("price")
            if shown_now is not None and price is not None and abs(price - shown_now) > 0.0051:
                mismatch = True
    return {"as_text": as_text, "mismatch": mismatch}


def bootstrap_diff(
    a: dict[str, float], b: dict[str, float], cluster: dict[str, str], draws: int = 2000
) -> tuple[float, float, float]:
    """Mean of a - b over shared tasks, with a cluster-bootstrap 95% CI."""
    ids = sorted(set(a) & set(b))
    by_c: dict[str, list[str]] = defaultdict(list)
    for t in ids:
        by_c[cluster[t]].append(t)
    cs = sorted(by_c)
    rng = random.Random(15)
    mean = _mean([a[t] - b[t] for t in ids])
    stats = []
    for _ in range(draws):
        pick = [t for _ in cs for t in by_c[cs[rng.randrange(len(cs))]]]
        stats.append(_mean([a[t] - b[t] for t in pick]))
    stats.sort()
    return mean, stats[int(0.025 * draws)], stats[int(0.975 * draws)]


def _logit(p: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def _sigmoid(z: float) -> float:
    z = max(min(z, 30.0), -30.0)
    return 1 / (1 + math.exp(-z))


def _fit_platt(
    xs: list[float], ys: list[float], iters: int = 100, ridge: float = 1.0
) -> tuple[float, float]:
    """Logistic y ~ a + b·logit(p) by Newton's method, with a small ridge pulling (a, b) toward
    (0, 1) (no recalibration) so copied or separable answers cannot make it diverge."""
    a, b = 0.0, 1.0
    for _ in range(iters):
        ga, gb = -ridge * a, -ridge * (b - 1.0)
        haa = hbb = ridge
        hab = 0.0
        for x, y in zip(xs, ys, strict=True):
            mu = _sigmoid(a + b * x)
            w = mu * (1 - mu)
            ga += y - mu
            gb += (y - mu) * x
            haa += w
            hab += w * x
            hbb += w * x * x
        det = haa * hbb - hab * hab
        da, db = (hbb * ga - hab * gb) / det, (haa * gb - hab * ga) / det
        a, b = a + da, b + db
        if abs(da) + abs(db) < 1e-9:
            break
    return a, b


def recalibrated(
    p: dict[str, float], y: dict[str, float], direction: dict[str, str], cluster: dict[str, str]
) -> dict[str, float]:
    """Per-direction Platt scaling, cross-fitted over 5 folds of clusters."""
    clusters = sorted({cluster[t] for t in p})
    fold = {c: i % 5 for i, c in enumerate(random.Random(7).sample(clusters, len(clusters)))}
    out = {}
    for k in range(5):
        for d in ("up", "down"):
            train = [t for t in p if fold[cluster[t]] != k and direction[t] == d]
            test = [t for t in p if fold[cluster[t]] == k and direction[t] == d]
            if not test:
                continue
            a, b = _fit_platt([_logit(p[t]) for t in train], [y[t] for t in train])
            for t in test:
                out[t] = _sigmoid(a + b * _logit(p[t]))
    return out


def arm_stats(
    p: dict[str, float], tasks: dict[str, Task], y: dict[str, float], extra: dict[str, Any]
) -> dict[str, Any]:
    ids = sorted(p)
    q = {t: market_baseline(tasks[t], {}) for t in ids}
    down = {t: float(tasks[t].facts["move_pts"]) < 0 for t in ids}
    head = {t: (tasks[t].facts["price_now"] if down[t] else 1 - tasks[t].facts["price_now"])
            for t in ids}  # fmt: skip
    out: dict[str, Any] = {
        "n": len(ids),
        "brier": _mean([(p[t] - y[t]) ** 2 for t in ids]),
        "brier_q": _mean([(q[t] - y[t]) ** 2 for t in ids]),
        "dir_corr": _corr([p[t] for t in ids], [float(down[t]) for t in ids]),
        "sd_p": _sd([p[t] for t in ids]),
        "copy_q_share": _mean([float(abs(p[t] - q[t]) <= 0.01) for t in ids]),
        "mean_abs_p_minus_q": _mean([abs(p[t] - q[t]) for t in ids]),
        **extra,
    }
    out["gap_to_q"] = out["brier"] - out["brier_q"]
    for d, flag in (("up", False), ("down", True)):
        sel = [t for t in ids if down[t] == flag]
        out[d] = {
            "n": len(sel),
            "mean_p": _mean([p[t] for t in sel]),
            "hold_rate": _mean([y[t] for t in sel]),
            "brier": _mean([(p[t] - y[t]) ** 2 for t in sel]),
            "brier_q": _mean([(q[t] - y[t]) ** 2 for t in sel]),
            "corr_p_y": _corr([p[t] for t in sel], [y[t] for t in sel]),
            "corr_p_q": _corr([p[t] for t in sel], [q[t] for t in sel]),
            "corr_p_headroom": _corr([p[t] for t in sel], [head[t] for t in sel]),
        }
    return out


def main() -> None:
    tasks_l, answers, _ = _load("validation")
    tasks = {t.task_id: t for t in tasks_l if t.kind == "spike_hold"}
    y = {t: answers[t].y for t in tasks}
    cluster = {t: tasks[t].cluster_id for t in tasks}
    direction = {t: "down" if tasks[t].facts["move_pts"] < 0 else "up" for t in tasks}
    q = {t: market_baseline(tasks[t], {}) for t in tasks}
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"arms": {}, "vs_q": {}}
    for arm, runs in {**ARMS, **REFERENCE}.items():
        if not all((PROBES / f"{r}.json").exists() for r in runs):
            continue
        per_task: dict[str, list[float]] = defaultdict(list)
        n_eps = malformed = over = as_text = mismatch = penalised = drops = claims = 0
        usd = 0.0
        for r in runs:
            rows, eps = load_run(r)
            for tid, row in rows.items():
                if tid not in tasks:
                    continue
                n_eps += 1
                usd += row.get("usd") or 0.0
                malformed += bool(row.get("malformed"))
                over += bool(row.get("budget_exceeded"))
                drops += row.get("dropped") or 0
                claims += row.get("claims") or 0
                if row.get("p") is not None:
                    per_task[tid].append(float(row["p"]))
                if tid in eps:
                    fl = episode_flags(eps[tid], tasks[tid])
                    as_text += fl["as_text"]
                    mismatch += fl["mismatch"]
                    nd, nc = row.get("dropped") or 0, row.get("claims") or 0
                    penalised += bool(row.get("budget_exceeded")) or (
                        nd + nc > 0 and nd > (nd + nc) / 2
                    )
        p = {t: _mean(v) for t, v in per_task.items()}
        within = [_sd(v) for v in per_task.values() if len(v) > 1]
        extra = {
            "episodes": n_eps, "usd": usd, "malformed": malformed / n_eps,
            "budget_exceeded": over / n_eps, "answered_as_text": as_text / n_eps,
            "frame_mismatch": mismatch, "penalised": penalised / n_eps,
            "verifier_drops_per_episode": drops / n_eps, "claims_kept_per_episode": claims / n_eps,
            "within_task_sd": _mean(within) if within else None,
        }  # fmt: skip
        st = arm_stats(p, tasks, y, extra)
        brier_p = {t: (p[t] - y[t]) ** 2 for t in p}
        brier_q = {t: (q[t] - y[t]) ** 2 for t in p}
        st["gap_ci"] = bootstrap_diff(brier_p, brier_q, cluster)
        rec = recalibrated(p, y, direction, cluster)
        st["recal_gap_ci"] = bootstrap_diff(
            {t: (rec[t] - y[t]) ** 2 for t in rec}, brier_q, cluster
        )
        blend = {t: (0.25 * p[t] + 0.75 * q[t] - y[t]) ** 2 for t in p}
        st["blend_gap_ci"] = bootstrap_diff(blend, brier_q, cluster)
        report["arms"][arm] = st
    spread: dict[str, Any] = {}
    for d in ("up", "down"):
        per_task = defaultdict(list)
        n = bad = 0
        for i in range(1, 7):
            name = f"p3_spread_{d}_s{i}"
            if not (PROBES / f"{name}.json").exists():
                continue
            rows, _ = load_run(name)
            for tid, row in rows.items():
                n += 1
                bad += bool(row.get("malformed"))
                if row.get("p") is not None:
                    per_task[tid].append(float(row["p"]))
        if not per_task:
            continue
        held = [p for t, v in per_task.items() if y[t] == 1 for p in v]
        not_held = [p for t, v in per_task.items() if y[t] == 0 for p in v]
        spread[d] = {
            "tasks": len(per_task), "episodes": n, "malformed": bad / n if n else None,
            "within_task_sd": _mean([_sd(v) for v in per_task.values() if len(v) > 1]),
            "share_correct_side_held": _mean([float(p > 0.5) for p in held]) if held else None,
            "share_correct_side_not_held": (
                _mean([float(p < 0.5) for p in not_held]) if not_held else None
            ),
            "tasks_with_a_correct_side_sample": sum(
                any((p > 0.5) == (y[t] == 1) for p in v) for t, v in per_task.items()
            ),
        }  # fmt: skip
    report["spread_probe"] = spread
    (OUT / "summary.json").write_text(json.dumps(report, indent=1, default=str))
    lines = [
        "| arm | n | Brier | q | gap [95% CI] | recal gap [CI] | blend gap [CI] | dir corr | "
        "down corr(p,q) | corr(p,y) up/down | corr(p,hr) up/down | sd(p) | copy-q | malformed "
        "| over budget | as text | mismatch | penalised | $ |",
        "|" + "---|" * 19,
    ]
    for arm, s in report["arms"].items():
        g, rg, bg = s["gap_ci"], s["recal_gap_ci"], s["blend_gap_ci"]
        lines.append(
            f"| {arm} | {s['n']} | {s['brier']:.3f} | {s['brier_q']:.3f} | "
            f"{g[0]:+.3f} [{g[1]:+.3f}, {g[2]:+.3f}] | {rg[0]:+.3f} [{rg[1]:+.3f}, {rg[2]:+.3f}] | "
            f"{bg[0]:+.4f} [{bg[1]:+.4f}, {bg[2]:+.4f}] | {s['dir_corr']:+.2f} | "
            f"{s['down']['corr_p_q']:+.2f} | "
            f"{s['up']['corr_p_y']:+.2f}/{s['down']['corr_p_y']:+.2f} | "
            f"{s['up']['corr_p_headroom']:+.2f}/{s['down']['corr_p_headroom']:+.2f} | "
            f"{s['sd_p']:.3f} | {s['copy_q_share']:.2f} | {s['malformed']:.2f} | "
            f"{s['budget_exceeded']:.2f} | {s['answered_as_text']:.2f} | {s['frame_mismatch']} | "
            f"{s['penalised']:.2f} | {s['usd']:.2f} |"
        )
    lines.append("")
    lines.append("Spread probe (thinking on, T = 1.0): " + json.dumps(spread, default=str))
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
