"""M15 eval harness: run a model (or a free baseline) over a split, score it, compare models.

Baselines need no model:
  market     resolve: the market price at as_of (the crowd's forecast); spike_hold: the
             train-split rate at which moves of that size and direction held.
  base_rate  the train-split rate of y = 1 for the task's kind.
Results go to evals/m15/results/<name>_<split>.json: one row per task (p, y, Brier, log loss,
reward, tool calls, verifier drops, cost) and a summary by kind and category.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import duckdb

from gridcase.rl.agent import AgentModel, Episode, run_episode
from gridcase.rl.reward import brier, ece, log_loss, paired_bootstrap, reward_for
from gridcase.rl.spend import Spend
from gridcase.rl.tasks import Answer, Task

BUCKETS = ((10, 15), (15, 20), (20, 30), (30, 101))


def bucket(move_pts: float) -> str:
    a = abs(move_pts)
    lo, hi = next(((lo, hi) for lo, hi in BUCKETS if lo <= a < hi), BUCKETS[-1])
    return f"{'up' if move_pts > 0 else 'down'}:{lo}-{hi}"


def base_rates(train: list[Task], answers: dict[str, Answer]) -> dict[str, float]:
    sums: dict[str, list[float]] = defaultdict(list)
    for t in train:
        y = answers[t.task_id].y
        sums[t.kind].append(y)
        if t.kind == "spike_hold":
            sums[bucket(float(t.facts["move_pts"]))].append(y)
    return {k: sum(v) / len(v) for k, v in sorted(sums.items())}


def market_baseline(task: Task, rates: dict[str, float]) -> float:
    if task.kind == "resolve":
        return float(task.facts["price_now"])
    key = bucket(float(task.facts["move_pts"]))
    return rates.get(key, rates.get("spike_hold", 0.5))


def base_rate_baseline(task: Task, rates: dict[str, float]) -> float:
    return rates.get(task.kind, 0.5)


def _row(
    task: Task, y: float, p: float | None, market_p: float, ep: Episode | None
) -> dict[str, Any]:
    malformed = p is None or (ep is not None and ep.malformed)
    penalised = ep is not None and ep.penalised
    row: dict[str, Any] = {
        "task_id": task.task_id, "kind": task.kind, "category": task.category,
        "cluster_id": task.cluster_id, "y": y, "p": p, "market_p": market_p,
        "brier": brier(p, y) if p is not None else None,
        "log_loss": log_loss(p, y) if p is not None else None,
        "market_brier": brier(market_p, y),
        "reward": reward_for(p, y, malformed, penalised),
    }  # fmt: skip
    if ep is not None:
        row |= {
            "tool_calls": ep.tool_calls, "turns": ep.turns, "claims": len(ep.claims),
            "dropped": len(ep.dropped), "budget_exceeded": ep.budget_exceeded,
            "malformed": ep.malformed, "error": ep.error, "usd": round(ep.usd, 6),
            "seconds": round(ep.seconds, 2),
        }  # fmt: skip
    return row


def _scored(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in rows if r["p"] is not None]
    n = len(rows)
    out: dict[str, Any] = {"n": n, "answered": len(valid)}
    if not n:
        return out
    # an unanswered task scores as p = 0.5 so a model cannot gain by skipping hard tasks
    b = [r["brier"] if r["p"] is not None else brier(0.5, r["y"]) for r in rows]
    mb = [r["market_brier"] for r in rows]
    out |= {
        "brier": sum(b) / n,
        "market_brier": sum(mb) / n,
        "skill_vs_market": 1 - (sum(b) / n) / (sum(mb) / n) if sum(mb) else None,
        "log_loss": sum(r["log_loss"] for r in valid) / len(valid) if valid else None,
        "ece": ece([r["p"] for r in valid], [r["y"] for r in valid]),
        "reward": sum(r["reward"] for r in rows) / n,
        "y_rate": sum(r["y"] for r in rows) / n,
    }
    if "tool_calls" in rows[0]:
        out |= {
            "tool_calls": sum(r["tool_calls"] for r in rows) / n,
            "malformed": sum(1 for r in rows if r["malformed"]) / n,
            "budget_exceeded": sum(1 for r in rows if r["budget_exceeded"]) / n,
            "verifier_dropped": sum(r["dropped"] for r in rows),
            "usd": sum(r["usd"] for r in rows),
        }
    return out


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = {"all": _scored(rows)}
    for key in ("kind", "category"):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            groups[r[key]].append(r)
        out[f"by_{key}"] = {k: _scored(v) for k, v in sorted(groups.items())}
    return out


def errored_task_ids(result: dict[str, Any]) -> set[str]:
    """Tasks lost to an API failure or the spend cap, not to the model's own answer."""
    return {r["task_id"] for r in result["rows"] if r.get("error")}


def merge_retry(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """`old` with the rows of `new` swapped in by task id, re-summarised."""
    fresh = {r["task_id"]: r for r in new["rows"]}
    rows = [fresh.get(r["task_id"], r) for r in old["rows"]]
    return {"name": old["name"], "summary": summarize(rows), "rows": rows}


def run_eval(
    name: str,
    tasks: list[Task],
    answers: dict[str, Answer],
    rates: dict[str, float],
    out_path: Path,
    conn: duckdb.DuckDBPyConnection | None = None,
    model: AgentModel | None = None,
    baseline: Callable[[Task, dict[str, float]], float] | None = None,
    spend: Spend | None = None,
    workers: int = 4,
) -> dict[str, Any]:
    """Exactly one of `model` (with `conn`) or `baseline`."""
    rows: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    lock = threading.Lock()

    def one(task: Task) -> None:
        y = answers[task.task_id].y
        mp = market_baseline(task, rates)
        if baseline is not None:
            row = _row(task, y, baseline(task, rates), mp, None)
            ep = None
        else:
            assert model is not None and conn is not None
            cur = conn.cursor()
            try:
                ep = run_episode(model, cur, task, spend)
            finally:
                cur.close()
            row = _row(task, y, ep.probability, mp, ep)
        with lock:
            rows.append(row)
            if ep is not None:
                claims = [c.model_dump() for c in ep.claims]
                episodes.append(
                    {"task_id": task.task_id, "transcript": ep.transcript,
                     "summary": ep.summary, "claims": claims, "dropped": ep.dropped}
                )  # fmt: skip

    if baseline is not None or workers <= 1:
        for t in tasks:
            one(t)
    else:
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(one, tasks))
    rows.sort(key=lambda r: r["task_id"])
    result = {"name": name, "summary": summarize(rows), "rows": rows}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=1, default=str))
    if episodes:
        ep_path = out_path.with_name(out_path.stem + "_episodes.jsonl")
        ep_path.write_text("".join(json.dumps(e, default=str) + "\n" for e in episodes))
    return result


def compare(a: dict[str, Any], b: dict[str, Any], draws: int = 2000) -> dict[str, float]:
    """Brier(a) - Brier(b) over shared tasks, with a cluster bootstrap 95% interval
    (negative = a is better). Unanswered tasks count as p = 0.5."""

    def briers(res: dict[str, Any]) -> dict[str, float]:
        return {
            r["task_id"]: r["brier"] if r["p"] is not None else brier(0.5, r["y"])
            for r in res["rows"]
        }

    cluster = {r["task_id"]: r["cluster_id"] for r in a["rows"]}
    mean, lo, hi = paired_bootstrap(briers(a), briers(b), cluster, draws=draws)
    return {"brier_diff": mean, "lo95": lo, "hi95": hi}
