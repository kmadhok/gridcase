"""`gridcase rl ...`: build and freeze the task sets, run evals, report.

gridcase rl tasks                 build tasks from data/backfill.duckdb, sample each split,
                                  freeze eval+validation (evals/m15/), write train tasks and
                                  the trade-backfill needs (data/rl/)
gridcase rl eval --model M --split S [--limit N] [--retry-errors]
                                  M: market | base_rate (free) | base (untuned small model,
                                  Tinker) | frontier (Anthropic) | ckpt:tinker://...
                                  --retry-errors reruns only the tasks an API failure or the
                                  spend cap ended, and merges them into the existing results
gridcase rl report                one table over every results file, with paired bootstraps
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC
from pathlib import Path
from typing import Any

from gridcase import config
from gridcase.rl.harness import (
    base_rate_baseline,
    base_rates,
    compare,
    errored_task_ids,
    market_baseline,
    merge_retry,
    run_eval,
)
from gridcase.rl.spend import Spend
from gridcase.rl.tasks import (
    Answer,
    Task,
    answers_sha,
    build_tasks,
    freeze,
    load_answers,
    load_manifest,
    sample_split,
    write_manifest,
)
from gridcase.store.db import connect

M15 = config.EVALS_DIR / "m15"
SIZES = {  # (spike_hold, resolve, tasks per cluster, Sports cap)
    "eval": (400, 200, 1, 0.4),
    "validation": (150, 100, 1, 0.4),
    "train": (4000, 2000, 3, 0.5),
}
SEED = 15


class FrozenMismatch(RuntimeError):
    """A rebuild produced a different eval/validation set than the committed one."""


def _check_frozen(frozen: list[Task], answers: dict[str, Answer]) -> None:
    """The committed manifest and answer hash are the benchmark; a rebuild must reproduce them
    exactly (it then only rewrites data/rl/answers.jsonl), or stop and say what changed."""
    path = M15 / "manifest.jsonl"
    if not path.exists():
        return
    old = {t.task_id: t for t in load_manifest(path)}
    new = {t.task_id: t for t in frozen}
    old_sha = (M15 / "answers.sha256").read_text().strip()
    new_sha = answers_sha({k: answers[k] for k in new})
    if old != new or old_sha != new_sha:
        sha_note = "same" if old_sha == new_sha else "differs"
        raise FrozenMismatch(
            f"rebuilt tasks differ from the committed eval set: {len(set(old) - set(new))} "
            f"missing, {len(set(new) - set(old))} new, answers sha {sha_note}; "
            "rerun with --force only to deliberately replace the benchmark"
        )


def build_and_freeze(force: bool = False) -> dict[str, Any]:
    con = connect(config.BACKFILL_DB, read_only=True)
    try:
        tasks, answers = build_tasks(con)
    finally:
        con.close()
    picked: dict[str, list[Task]] = {}
    for split, (n_spike, n_res, per_cluster, cap) in SIZES.items():
        pool = [t for t in tasks if t.split == split]
        spikes = sample_split(
            [t for t in pool if t.kind == "spike_hold"], n_spike, per_cluster, cap, SEED
        )
        res = sample_split([t for t in pool if t.kind == "resolve"], n_res, per_cluster, cap, SEED)
        picked[split] = spikes + res
    frozen = picked["eval"] + picked["validation"]
    if not force:
        _check_frozen(frozen, answers)
    sha = freeze(frozen, answers, M15, config.RL_DIR)
    write_manifest(picked["train"], M15 / "train_manifest.jsonl")
    train_answers = {t.task_id: answers[t.task_id] for t in picked["train"]}
    (config.RL_DIR / "train_answers.jsonl").write_text(
        "".join(a.model_dump_json() + "\n" for _, a in sorted(train_answers.items()))
    )
    needed: dict[str, int] = {}
    for t in [*frozen, *picked["train"]]:
        start = int(t.as_of.replace(tzinfo=UTC).timestamp()) - 7 * 86400  # as_of is naive UTC
        needed[t.condition_id] = min(needed.get(t.condition_id, start), start)
    (config.RL_DIR / "trades_needed.json").write_text(json.dumps(needed))
    rates = base_rates(picked["train"], train_answers)
    (M15 / "base_rates.json").write_text(json.dumps(rates, indent=1))
    pool_counts = Counter((t.split, t.kind) for t in tasks)
    summary = {
        "answers_sha256": sha,
        "pool": {f"{s}/{k}": n for (s, k), n in sorted(pool_counts.items())},
        "picked": {
            s: {
                "n": len(v),
                "by_kind": dict(Counter(t.kind for t in v)),
                "by_category": dict(Counter(t.category for t in v).most_common()),
                "clusters": len({t.cluster_id for t in v}),
                "y_rate": round(sum(answers[t.task_id].y for t in v) / len(v), 3) if v else None,
            }
            for s, v in picked.items()
        },
        "trade_markets": len(needed),
        "base_rates": rates,
    }
    (M15 / "tasks_summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def _load(split: str) -> tuple[list[Task], dict[str, Answer], dict[str, float]]:
    if split == "train":
        tasks = load_manifest(M15 / "train_manifest.jsonl")
        answers = load_answers(config.RL_DIR / "train_answers.jsonl")
    else:
        tasks = [t for t in load_manifest(M15 / "manifest.jsonl") if t.split == split]
        answers = load_answers(config.RL_DIR / "answers.jsonl")
    rates = json.loads((M15 / "base_rates.json").read_text())
    return tasks, answers, rates


def evaluate(
    model: str, split: str, limit: int | None, workers: int, retry_errors: bool = False
) -> dict[str, Any]:
    tasks, answers, rates = _load(split)
    tasks = tasks[: limit or None]
    name = model.replace("ckpt:tinker://", "ckpt-").replace("/", "_").replace(":", "_")
    out = M15 / "results" / f"{name}_{split}.json"
    final, old = out, None
    if retry_errors:
        old = json.loads(out.read_text())
        retry = errored_task_ids(old)
        tasks = [t for t in tasks if t.task_id in retry]
        out = out.with_name(out.stem + "_retry.json")
    if model in ("market", "base_rate"):
        fn = market_baseline if model == "market" else base_rate_baseline
        summary: dict[str, Any] = run_eval(model, tasks, answers, rates, out, baseline=fn)[
            "summary"
        ]
        return dict(summary["all"])
    agent: Any
    if model == "frontier":
        if not config.has_anthropic_api_key():
            raise SystemExit("ANTHROPIC_API_KEY is not set (.env): the frontier eval is paid")
        from gridcase.rl.models import AnthropicAgent

        agent = AnthropicAgent()
    else:
        if not config.has_tinker_api_key():
            raise SystemExit("TINKER_API_KEY is not set (.env): small-model evals run on Tinker")
        from gridcase.rl.models import TinkerAgent

        path = model.removeprefix("ckpt:") if model.startswith("ckpt:") else None
        agent = TinkerAgent(model_path=path)
    spend = Spend(config.RL_DIR / "spend.jsonl")
    con = connect(config.BACKFILL_DB, read_only=True)
    try:
        res = run_eval(name, tasks, answers, rates, out, conn=con, model=agent, spend=spend,
                       workers=workers)  # fmt: skip
    finally:
        con.close()
    if old is not None:
        res = _merge_retry_files(old, res, final, out)
    return dict(res["summary"]["all"])


def _merge_retry_files(
    old: dict[str, Any], new: dict[str, Any], final: Path, retry: Path
) -> dict[str, Any]:
    merged = merge_retry(old, new)
    final.write_text(json.dumps(merged, indent=1, default=str))
    eps_final = final.with_name(final.stem + "_episodes.jsonl")
    eps_retry = retry.with_name(retry.stem + "_episodes.jsonl")
    if eps_retry.exists():
        redone = {r["task_id"] for r in new["rows"]}
        kept = [
            line for line in eps_final.read_text().splitlines(keepends=True)
            if json.loads(line)["task_id"] not in redone
        ] if eps_final.exists() else []  # fmt: skip
        eps_final.write_text("".join(kept) + eps_retry.read_text())
        eps_retry.unlink()
    retry.unlink(missing_ok=True)
    return merged


def report() -> str:
    loaded = {p.stem: json.loads(p.read_text()) for p in sorted((M15 / "results").glob("*.json"))}
    # eval results only: the trainer writes train_history.json and train_state.json here too
    results = {k: v for k, v in loaded.items() if isinstance(v, dict) and "summary" in v}
    lines = [
        "| run | n | answered | Brier | market Brier | skill vs market | log loss | ECE "
        "| reward | $ |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    def f(x: Any, d: int = 4) -> str:
        return "–" if x is None else f"{x:.{d}f}"

    for name, r in results.items():
        a = r["summary"]["all"]
        lines.append(
            f"| {name} | {a['n']} | {a['answered']} | {f(a.get('brier'))} | "
            f"{f(a.get('market_brier'))} | {f(a.get('skill_vs_market'), 3)} | "
            f"{f(a.get('log_loss'))} | {f(a.get('ece'))} | {f(a.get('reward'))} | "
            f"{f(a.get('usd'), 2)} |"
        )
    pairs = [
        (a, b) for a in results for b in results if a < b and a.split("_")[-1] == b.split("_")[-1]
    ]
    if pairs:
        lines += ["", "| a vs b (same split) | Brier(a) − Brier(b) | 95% CI (cluster bootstrap) |",
                  "|---|---|---|"]  # fmt: skip
        for a, b in pairs:
            c = compare(results[a], results[b])
            lines.append(
                f"| {a} vs {b} | {c['brier_diff']:+.4f} | [{c['lo95']:+.4f}, {c['hi95']:+.4f}] |"
            )
    text = "\n".join(lines) + "\n"
    (M15 / "results" / "summary.md").write_text(text)
    return text


def main(args: Any) -> int:
    if args.rl_cmd == "tasks":
        print(json.dumps(build_and_freeze(force=args.force), indent=1))
    elif args.rl_cmd == "eval":
        res = evaluate(args.model, args.split, args.limit, args.workers, args.retry_errors)
        print(json.dumps(res, indent=1))
    elif args.rl_cmd == "report":
        print(report())
    else:
        from gridcase.rl.train import train_main

        return train_main(args)
    return 0


def results_dir() -> Path:
    return M15 / "results"
