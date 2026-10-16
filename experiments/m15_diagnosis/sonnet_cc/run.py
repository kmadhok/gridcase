"""Run Sonnet through the Claude Code subscription on M15 tasks, with the agent's own tools.

    uv run python experiments/m15_diagnosis/sonnet_cc/run.py --split holdout --kind spike_hold \
        --name sonnet_cc_holdout [--limit 20] [--workers 6]

Each task is one `claude -p --model sonnet` session in an empty directory: the M15 system
prompt replaces Claude Code's, every built-in tool is off, and the only tools are the task's
(server.py, over MCP). The prompt is prompt_for(task, framing, facts receipt id), as for Qwen.
No API key and no ledger spend: the subscription pays. Resumable (finished tasks are skipped).
Writes data/rl/diagnosis/sonnet_cc/<name>.json (run_eval's format, so holdout_grade.py reads it),
<name>_episodes.jsonl and per-task Claude Code metadata.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from gridcase import config
from gridcase.explain.schema import Claim
from gridcase.rl.agent import (
    SYSTEM,
    Episode,
    _second_outcome,
    facts_receipt,
    frame_for,
    prompt_for,
)
from gridcase.rl.cli import HOLDOUT, M15, _load
from gridcase.rl.harness import _row, market_baseline, summarize
from gridcase.rl.tasks import Task
from gridcase.store.db import connect

ROOT = Path(__file__).resolve().parents[3]
SERVER = Path(__file__).with_name("server.py")
PYTHON = ROOT / ".venv" / "bin" / "python"
OUT = ROOT / "data" / "rl" / "diagnosis" / "sonnet_cc"


def manifest_for(split: str) -> Path:
    return HOLDOUT / "manifest.jsonl" if split == "holdout" else M15 / "manifest.jsonl"


def run_task(task: Task, prompt: str, a: argparse.Namespace, work: Path) -> dict[str, Any]:
    ep_file = work / f"{task.task_id.replace(':', '_')}.episode.json"
    meta_file = ep_file.with_suffix(".meta.json")
    if meta_file.exists() and ep_file.exists():
        cached: dict[str, Any] = json.loads(meta_file.read_text())
        return cached
    mcp = {"mcpServers": {"m15": {
        "command": str(PYTHON), "args": [str(SERVER)],
        "env": {"M15_TASK_ID": task.task_id, "M15_MANIFEST": str(manifest_for(a.split)),
                "M15_FRAMING": a.framing, "M15_OUT": str(ep_file)},
    }}}  # fmt: skip
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "mcp.json"
        cfg.write_text(json.dumps(mcp))
        cmd = [
            "claude", "-p", "--model", a.model, "--system-prompt", SYSTEM, "--tools", "",
            "--mcp-config", str(cfg), "--strict-mcp-config", "--allowedTools", "mcp__m15__*",
            "--permission-mode", "bypassPermissions", "--output-format", "stream-json",
            "--verbose", "--no-session-persistence", "--disable-slash-commands",
        ]  # fmt: skip
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, input=prompt, text=True, capture_output=True, cwd=tmp, timeout=a.timeout
            )
            lines = proc.stdout.splitlines()
            err = proc.stderr[-500:] if proc.returncode else None
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode()
            lines, err = out.splitlines(), f"timeout after {a.timeout}s"
    meta: dict[str, Any] = {"task_id": task.task_id, "seconds": time.perf_counter() - t0,
                            "error": err}  # fmt: skip
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            meta["model"] = ev.get("model")
            meta["tools"] = ev.get("tools")
        if ev.get("type") == "result":
            meta |= {k: ev.get(k) for k in ("num_turns", "is_error", "total_cost_usd", "usage",
                                            "subtype")}  # fmt: skip
            meta["final_text"] = str(ev.get("result") or "")[-400:]
    meta_file.write_text(json.dumps(meta, default=str))
    return meta


def episode_from(path: Path, task: Task, meta: dict[str, Any]) -> Episode:
    ep = Episode(task_id=task.task_id, model=str(meta.get("model") or "claude-code:sonnet"))
    if path.exists():
        d = json.loads(path.read_text())
        ep.probability = d["probability"]
        ep.summary = d["summary"]
        ep.claims = [Claim(**c) for c in d["claims"]]
        ep.dropped = d["dropped"]
        ep.tool_calls = d["tool_calls"]
        ep.budget_exceeded = d["budget_exceeded"]
        ep.malformed = d["malformed"] or not d["submitted"] or d["probability"] is None
        ep.transcript = d["transcript"]
    else:
        ep.malformed = True
    ep.turns = int(meta.get("num_turns") or 0)
    ep.seconds = float(meta.get("seconds") or 0.0)
    ep.error = meta.get("error")
    return ep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="holdout", choices=["holdout", "validation", "eval"])
    ap.add_argument("--kind", default="spike_hold", choices=["spike_hold", "resolve", "all"])
    ap.add_argument("--framing", default="bd", choices=["a", "bd"])
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--name", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    tasks, answers, rates = _load(a.split)
    if a.kind != "all":
        tasks = [t for t in tasks if t.kind == a.kind]
    tasks = tasks[: a.limit or None]
    work = OUT / a.name
    work.mkdir(parents=True, exist_ok=True)
    con = connect(config.BACKFILL_DB, read_only=True)
    prompts = {}
    for t in tasks:
        frame = frame_for(t, a.framing)
        if frame.flip:
            frame = replace(frame, outcome=_second_outcome(con, t))
        prompts[t.task_id] = prompt_for(t, a.framing, facts_receipt(t, frame).receipt_id)
    con.close()

    done = 0

    def one(t: Task) -> dict[str, Any]:
        nonlocal done
        meta = run_task(t, prompts[t.task_id], a, work)
        done += 1
        if done % 25 == 0 or done == len(tasks):
            print(f"{done}/{len(tasks)}", flush=True)
        return meta

    with ThreadPoolExecutor(a.workers) as pool:
        metas = dict(zip([t.task_id for t in tasks], pool.map(one, tasks), strict=True))

    rows, eps = [], []
    for t in tasks:
        path = work / f"{t.task_id.replace(':', '_')}.episode.json"
        ep = episode_from(path, t, metas[t.task_id])
        rows.append(_row(t, answers[t.task_id].y, ep.probability, market_baseline(t, rates), ep))
        eps.append({"task_id": t.task_id, "transcript": ep.transcript, "summary": ep.summary,
                    "claims": [c.model_dump() for c in ep.claims], "dropped": ep.dropped,
                    "model": ep.model})  # fmt: skip
    rows.sort(key=lambda r: r["task_id"])
    result = {"name": a.name, "summary": summarize(rows), "rows": rows}
    (OUT / f"{a.name}.json").write_text(json.dumps(result, indent=1, default=str))
    (OUT / f"{a.name}_episodes.jsonl").write_text(
        "".join(json.dumps(e, default=str) + "\n" for e in eps)
    )
    models = sorted({str(m.get("model")) for m in metas.values()})
    errors = sum(1 for m in metas.values() if m.get("error") or m.get("is_error"))
    print(json.dumps({"summary": result["summary"]["all"], "models": models,
                      "errors": errors}, indent=1, default=str))  # fmt: skip


if __name__ == "__main__":
    main()
