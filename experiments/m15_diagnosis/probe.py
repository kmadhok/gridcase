"""M15 diagnosis, phase 4: paid probes of the untuned (or a tuned) Qwen3-8B on validation tasks.

    uv run python experiments/m15_diagnosis/probe.py --name think_on --renderer qwen3 \
        --max-tokens 4096 [--variant base] [--framing bd] [--kind spike_hold] [--direction down] \
        [--limit 60] [--top-p 0.95 --top-k 20] [--phase reserve]

Runs the normal agent loop on the validation split (never eval or holdout), with an optional
renderer (thinking on/off), sampling settings, framing (config.RL_FRAMING) and prompt variant,
and writes data/rl/diagnosis/probes/<name>.json plus <name>_episodes.jsonl (transcripts keep the
model's text and thinking). Spend goes through the M15 ledger under --phase (small_eval).
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


Prompt = Callable[[Task, str, str | None], str]  # (task, framing, facts receipt id)


def _spike(text: Callable[[Task], str]) -> Prompt:
    """A prompt variant that rewrites only the spike_hold question (the original framing)."""

    def prompt(task: Task, framing: str = "a", facts_id: str | None = None) -> str:
        if task.kind != "spike_hold":
            return BASE_PROMPT(task, framing, facts_id)
        when = task.as_of.strftime("%Y-%m-%d %H:%M UTC")
        head = f"Market condition_id: {task.condition_id}\nas_of: {when}\n\n"
        cite = f" The numbers above are receipt {facts_id}." if facts_id else ""
        return head + text(task) + cite

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


# Worked examples for the few-shot arm (docs/M15_FINETUNING_GUIDE.md §5 P3): invented, not
# drawn from any task, written in the BD framing; one held move, one launch placeholder, one
# thin-market blip that reverted (the up-move inflation the probes found).
EXAMPLES = (
    "\n\nWorked examples (invented, for the reasoning only):\n"
    "1. A market's price moved from 0.62 to 0.90 (+28.0 points); market-implied 0.900. The series "
    "sat near 0.62 for days, then stepped up over two hours on heavy volume, and markets in the "
    "same event moved the same way: real news, close to a boundary. Answer about 0.88.\n"
    "2. The price of a market's second outcome moved from 0.50 to 0.93 (+43.0 points); "
    "market-implied 0.930. hist.market shows the market started 3 hours before as_of and the "
    "series begins at exactly 0.50: that was a launch placeholder, not a price, so the new level "
    "is the first real price and almost always stays. Answer about 0.93.\n"
    "3. A market's price moved from 0.30 to 0.52 (+22.0 points); market-implied 0.520. One "
    "10-minute point carries the move, flow shows a handful of trades, siblings did not move: a "
    "thin-market blip that usually fades. Answer about 0.30."
)


def _fewshot(task: Task, framing: str = "bd", facts_id: str | None = None) -> str:
    text = BASE_PROMPT(task, framing, facts_id)
    return text + EXAMPLES if task.kind == "spike_hold" else text


VARIANTS: dict[str, Prompt] = {
    "base": BASE_PROMPT,
    # spell out the threshold and its side so "still there" cannot be read as "price high"
    "explicit": _spike(_explicit),
    "fewshot": _fewshot,
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
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--framing", default=config.RL_FRAMING, choices=["a", "bd"])
    ap.add_argument("--top-p", type=float, default=None)
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--phase", default="small_eval")
    a = ap.parse_args()

    tasks, answers, rates = _load("validation")
    if a.kind != "all":
        tasks = [t for t in tasks if t.kind == a.kind]
    if a.direction != "all":
        want_up = a.direction == "up"
        tasks = [t for t in tasks if t.kind != "spike_hold" or (t.facts["move_pts"] > 0) == want_up]
    tasks = tasks[a.offset :][: a.limit or None]

    agent_mod.prompt_for = VARIANTS[a.variant]  # type: ignore[assignment]  # looked up per call
    config.RL_FRAMING = a.framing  # run_episode's default framing
    model = TinkerAgent(
        model_path=a.model_path,
        phase=a.phase,
        renderer_name=a.renderer,
        temperature=a.temperature,
        max_tokens=a.max_tokens,
        top_p=a.top_p,
        top_k=a.top_k,
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
