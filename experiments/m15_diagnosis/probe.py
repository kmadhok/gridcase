"""M15 diagnosis, phase 4: paid probes of the untuned (or a tuned) Qwen3-8B on validation tasks.

    uv run python experiments/m15_diagnosis/probe.py --name think_on --renderer qwen3 \
        --max-tokens 4096 [--variant base] [--kind spike_hold] [--direction down] [--limit 60]

Runs the normal agent loop on the validation split (never eval or holdout), with an optional
renderer (thinking on/off), sampling temperature and prompt variant, and writes
data/rl/diagnosis/probes/<name>.json plus <name>_episodes.jsonl (transcripts now keep the
model's text and thinking). Spend goes through the M15 ledger under small_eval.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from gridcase import config
from gridcase.rl import agent as agent_mod
from gridcase.rl.cli import _load
from gridcase.rl.diagnose import spike_diagnostics
from gridcase.rl.harness import run_eval
from gridcase.rl.models import TinkerAgent
from gridcase.rl.spend import Spend
from gridcase.rl.tasks import Task
from gridcase.store.db import connect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "rl" / "diagnosis" / "probes"
BASE_PROMPT = agent_mod.prompt_for


def _later(task: Task) -> str:
    return (task.as_of + timedelta(hours=int(task.facts["horizon_hours"]))).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _spike(text: Callable[[Task], str]) -> Callable[[Task], str]:
    """A prompt variant that rewrites only the spike_hold question."""

    def prompt(task: Task) -> str:
        if task.kind != "spike_hold":
            return BASE_PROMPT(task)
        when = task.as_of.strftime("%Y-%m-%d %H:%M UTC")
        head = f"Market condition_id: {task.condition_id}\nas_of: {when}\n\n"
        return head + text(task)

    return prompt


def _explicit(task: Task) -> str:
    f = task.facts
    before, now = float(f["price_before"]), float(f["price_now"])
    half = round((before + now) / 2, 3)
    side = "at or above" if now > before else "at or below"
    return (
        f"In the last {f['window_hours']} hours the market's price moved from {before} to {now} "
        f"({f['move_pts']:+} points). Question: what is the probability that {f['horizon_hours']} "
        f"hours later, at {_later(task)}, the price is still {side} {half} (that is, at least "
        "half of this move has not been reversed)? Investigate why it moved, then submit your "
        "probability."
    )


VARIANTS: dict[str, Callable[[Task], str]] = {
    "base": BASE_PROMPT,
    # spell out the threshold and its side so "still there" cannot be read as "price high"
    "explicit": _spike(_explicit),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--renderer", default=config.RL_RENDERER)
    ap.add_argument("--model-path", default=None, help="tinker:// checkpoint; default base")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--max-tokens", type=int, default=1024)
    ap.add_argument("--variant", default="base", choices=sorted(VARIANTS))
    ap.add_argument("--kind", default="spike_hold", choices=["spike_hold", "resolve", "all"])
    ap.add_argument("--direction", default="all", choices=["up", "down", "all"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()

    tasks, answers, rates = _load("validation")
    if a.kind != "all":
        tasks = [t for t in tasks if t.kind == a.kind]
    if a.direction != "all":
        want_up = a.direction == "up"
        tasks = [t for t in tasks if t.kind != "spike_hold" or (t.facts["move_pts"] > 0) == want_up]
    tasks = tasks[: a.limit or None]

    agent_mod.prompt_for = VARIANTS[a.variant]  # type: ignore[assignment]  # looked up per call
    model = TinkerAgent(
        model_path=a.model_path,
        renderer_name=a.renderer,
        temperature=a.temperature,
        max_tokens=a.max_tokens,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{a.name}.json"
    con = connect(config.BACKFILL_DB, read_only=True)
    try:
        res = run_eval(
            a.name, tasks, answers, rates, out, conn=con, model=model,
            spend=Spend(config.RL_DIR / "spend.jsonl"), workers=a.workers,
        )  # fmt: skip
    finally:
        con.close()
    diag = spike_diagnostics(res["rows"], {t.task_id: t for t in tasks})
    meta = {"args": vars(a), "summary": res["summary"]["all"], "spike": diag}
    (OUT / f"{a.name}_meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
