"""M15 step 2: read a results file and say whether spike_hold answers follow the evidence.

    gridcase rl diagnose --file evals/m15/results/val_step6_validation.json

For spike_hold rows: mean answer after up- and down-moves, the correlation of the answer with
direction (down = 1; the train hold rates say it should be positive) and with headroom (the
distance from price_now to the boundary the move points to; hold rates fall as it grows, so
the correlation should be negative), the number of distinct answers and their spread, and the
Brier of the rows against the best constant on the same rows. Offline: it reads only the
results file and the frozen manifest.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from gridcase.rl.reward import brier
from gridcase.rl.tasks import Task, load_manifest


def headroom(task: Task) -> float:
    p = float(task.facts["price_now"])
    return 1.0 - p if float(task.facts["move_pts"]) > 0 else p


def _corr(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return cov / math.sqrt(vx * vy) if vx > 1e-12 and vy > 1e-12 else 0.0


def spike_diagnostics(rows: list[dict[str, Any]], tasks: dict[str, Task]) -> dict[str, Any]:
    picked = [
        (r, tasks[r["task_id"]])
        for r in rows
        if r.get("p") is not None and tasks[r["task_id"]].kind == "spike_hold"
    ]
    if not picked:
        return {"n": 0}
    ps = [float(r["p"]) for r, _ in picked]
    ys = [float(r["y"]) for r, _ in picked]
    down = [1.0 if float(t.facts["move_pts"]) < 0 else 0.0 for _, t in picked]
    hr = [headroom(t) for _, t in picked]
    y_bar = sum(ys) / len(ys)
    mean_p = sum(ps) / len(ps)

    def mean_where(flag: float) -> float | None:
        sel = [p for p, d in zip(ps, down, strict=True) if d == flag]
        return sum(sel) / len(sel) if sel else None

    return {
        "n": len(picked),
        "mean_p_up": mean_where(0.0),
        "mean_p_down": mean_where(1.0),
        "direction_corr": _corr(ps, down),
        "headroom_corr": _corr(ps, hr),
        "distinct_p": len({round(p, 2) for p in ps}),
        "p_std": math.sqrt(sum((p - mean_p) ** 2 for p in ps) / len(ps)),
        "brier": sum(brier(p, y) for p, y in zip(ps, ys, strict=True)) / len(ps),
        "best_constant_brier": sum(brier(y_bar, y) for y in ys) / len(ys),
        "y_rate": y_bar,
    }


def diagnose(path: Path, manifest: Path) -> dict[str, Any]:
    res = json.loads(path.read_text())
    tasks = {t.task_id: t for t in load_manifest(manifest)}
    return spike_diagnostics(res["rows"], tasks)
