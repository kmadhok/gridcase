"""M15 diagnosis, phase 0: one fact table of every evaluated episode across runs.

    uv run python experiments/m15_diagnosis/build_facts.py

Joins each results file's rows with the frozen manifest (task facts) and, where the run saved
them, its episodes (tool sequence, summary, claims). Writes data/rl/diagnosis/episodes.jsonl
(one row per run x task) and data/rl/diagnosis/runs.json (per-run spike diagnostics).
Offline; reads only committed results and the manifests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gridcase.rl.diagnose import headroom, spike_diagnostics
from gridcase.rl.harness import bucket
from gridcase.rl.tasks import load_manifest

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "evals" / "m15" / "results"
OUT = ROOT / "data" / "rl" / "diagnosis"

# run label -> results file stem (episodes file is <stem>_episodes.jsonl when present)
RUNS = {
    "base_eval": "base_eval",
    "frontier_eval": "frontier_eval",
    "tuned39_eval": (
        "ckpt-176a00f3-4ce7-5310-aee8-ec3ada524ce8_train_0_sampler_weights_ckpt-39_eval"
    ),
    "scripted_market_eval": "scripted_market_eval",
    "market_eval": "market_eval",
    "bucket_rate_eval": "bucket_rate_eval",
    "base_validation": "base_validation",
    "run1_step10_validation": "val_step10_validation",
    "run1_step20_validation": "val_step20_validation",
    "run1_step30_validation": "val_step30_validation",
    "run1_step39_validation": "val_step39_validation",
    "pilot_step6_validation": "val_step6_validation",
    "market_validation": "market_validation",
    "bucket_rate_validation": "bucket_rate_validation",
}


def episodes(stem: str) -> dict[str, dict[str, Any]]:
    path = RESULTS / f"{stem}_episodes.jsonl"
    if not path.exists():
        return {}
    out = {}
    for line in path.open():
        e = json.loads(line)
        out[e["task_id"]] = e
    return out


def main() -> None:
    tasks = {t.task_id: t for t in load_manifest(ROOT / "evals" / "m15" / "manifest.jsonl")}
    OUT.mkdir(parents=True, exist_ok=True)
    runs: dict[str, Any] = {}
    with (OUT / "episodes.jsonl").open("w") as f:
        for run, stem in RUNS.items():
            res = json.loads((RESULTS / f"{stem}.json").read_text())
            eps = episodes(stem)
            runs[run] = {
                "file": f"evals/m15/results/{stem}.json",
                "episodes_file": f"evals/m15/results/{stem}_episodes.jsonl" if eps else None,
                "summary_all": res["summary"].get("all"),
                "spike": spike_diagnostics(res["rows"], tasks),
            }
            for r in res["rows"]:
                t = tasks[r["task_id"]]
                row: dict[str, Any] = {"run": run, **r, "split": t.split, "facts": t.facts}
                if t.kind == "spike_hold":
                    mv = float(t.facts["move_pts"])
                    row |= {
                        "direction": "up" if mv > 0 else "down",
                        "bucket": bucket(mv),
                        "headroom": round(headroom(t), 4),
                    }
                e = eps.get(r["task_id"])
                if e:
                    tr = e["transcript"]
                    row |= {
                        "question": tr[0].get("content"),
                        "tool_seq": [c[0] for s in tr if s["role"] == "tool" for c in s["calls"]],
                        "summary": e.get("summary"),
                        "claims": [c["text"] for c in e.get("claims") or []],
                        "dropped_claims": e.get("dropped"),
                    }
                f.write(json.dumps(row) + "\n")
    (OUT / "runs.json").write_text(json.dumps(runs, indent=1))
    for run, v in runs.items():
        s = v["spike"]
        print(
            f"{run:26s} brier={v['summary_all']['brier']:.4f} spike_n={s.get('n')} "
            f"dir_corr={s.get('direction_corr', 0):+.2f} "
            f"hr_corr={s.get('headroom_corr', 0):+.2f} spike_brier={s.get('brier', 0):.3f} "
            f"eps={'y' if v['episodes_file'] else 'n'}"
        )


if __name__ == "__main__":
    main()
