"""Free check (D2): re-score saved eval episodes under the new verifier rules, offline.

For each saved eval, from its episodes file and its .json rows:
  old        penalised as recorded (budget exceeded, or > half the claims dropped; the summary
             was then an uncited claim, dropped whenever it stated a number)
  no_summary the same, with the summary's own drop removed (D2a: the summary is not a claim)
  strict     no_summary, plus kept non-summary claims re-verified with verify(strict=True)
             against receipts rebuilt from the transcript's tool results (D2b)
Old-rule drops stay dropped (strict matching only removes matches). The task-facts receipt did
not exist when these ran, so no claim here cites it.

    uv run python experiments/m15_diagnosis/free_rescore_d2.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from gridcase.explain.schema import Claim, Conclusion  # noqa: E402
from gridcase.explain.verifier import verify  # noqa: E402
from gridcase.ledger.schema import Receipt  # noqa: E402
from gridcase.rl.reward import MALFORMED, brier  # noqa: E402

RESULTS = ROOT / "evals/m15/results"
OUT = ROOT / "data/rl/diagnosis/free"
FILES = ("base_eval", "frontier_eval", "base_validation", "val_step6_validation")
TRUNC = " …(truncated)"
EPOCH = datetime(2026, 1, 1)


def receipts_of(transcript: list[dict[str, Any]]) -> tuple[dict[str, Receipt], set[str], int]:
    """Receipts rebuilt from tool results; ids whose output was truncated (unparseable)."""
    out: dict[str, Receipt] = {}
    truncated: set[str] = set()
    n_trunc = 0
    for step in transcript:
        if step.get("role") != "tool":
            continue
        names = [c[0] for c in step.get("calls", [])]
        for i, (_cid, content) in enumerate(step.get("results", [])):
            tool = names[i] if i < len(names) else "?"
            if content.endswith(TRUNC):
                n_trunc += 1
                rid = content[len('{"receipt_id": "') :].split('"', 1)[0]
                if rid:
                    truncated.add(rid)
                continue
            try:
                d = json.loads(content)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict) or "receipt_id" not in d:
                continue  # a tool error message, not a receipt
            out[d["receipt_id"]] = Receipt(
                receipt_id=d["receipt_id"],
                tool=tool,
                inputs={},
                as_of=EPOCH,
                data_version=None,
                output=d.get("output") or {},
                effect_size=None,
                runtime_ms=0,
            )
    return out, truncated, n_trunc


def is_summary(drop: str, summary: str) -> bool:
    return bool(summary) and drop.startswith(summary[:60] + ":")


def penalised(budget: bool, kept: int, dropped: int) -> bool:
    n = kept + dropped
    return budget or (n > 0 and dropped > n / 2)


def facts_values(task_id: str, facts: dict[str, dict[str, Any]]) -> list[float]:
    """What the D2 task.facts receipt would hold (the prompt's numbers, plus q under bd)."""
    f = facts.get(task_id, {})
    vals = [float(v) for v in f.values() if isinstance(v, int | float)]
    if "move_pts" in f:
        pn = float(f["price_now"])
        vals.append(pn if float(f["move_pts"]) > 0 else 1 - pn)
    return vals


def drop_number(drop: str) -> float | None:
    """The first bad number of a 'number X not in cited receipts' drop."""
    tail = drop.rsplit(": number ", 1)
    if len(tail) != 2 or not tail[1].endswith(" not in cited receipts"):
        return None
    return float(tail[1].split(" ", 1)[0])


def rescore(name: str, facts: dict[str, dict[str, Any]], kind: str | None = None) -> dict[str, Any]:
    rows = {r["task_id"]: r for r in json.loads((RESULTS / f"{name}.json").read_text())["rows"]}
    eps = [json.loads(line) for line in (RESULTS / f"{name}_episodes.jsonl").open()]
    acc: dict[str, Any] = {
        "file": name,
        "episodes": 0,
        "answered": 0,
        "pen_old_recorded": 0,
        "pen_old": 0,
        "pen_nosum": 0,
        "pen_strict": 0,
        "pen_strict_pessimistic": 0,
        "kept_ns": 0,
        "kept_ns_checked": 0,
        "kept_ns_strict_drop": 0,
        "kept_ns_skipped_trunc": 0,
        "kept_ns_uncited": 0,
        "truncated_results": 0,
        "summary_dropped": 0,
        "reward_old": 0.0,
        "reward_nosum": 0.0,
        "reward_strict": 0.0,
        "examples": [],
    }
    acc["rescued_by_facts"] = 0
    acc["pen_facts_ub"] = 0
    for ep in eps:
        r = rows[ep["task_id"]]
        if kind is not None and r["kind"] != kind:
            continue
        acc["episodes"] += 1
        malformed = r["p"] is None or r.get("malformed")
        budget = bool(r.get("budget_exceeded"))
        summary = ep.get("summary") or ""
        kept = ep.get("claims") or []
        dropped = ep.get("dropped") or []
        receipts, trunc_ids, n_trunc = receipts_of(ep.get("transcript") or [])
        acc["truncated_results"] += n_trunc
        sum_drops = [d for d in dropped if is_summary(d, summary)]
        acc["summary_dropped"] += bool(sum_drops)
        other_drops = len(dropped) - len(sum_drops)
        # kept claims never include the summary (old _finish removed it), but guard anyway
        kept_ns = [c for c in kept if c["text"] != summary or c.get("receipt_ids")]
        new_drops, skipped = 0, 0
        strict_drop_msgs: list[str] = []
        for c in kept_ns:
            acc["kept_ns"] += 1
            ids = c.get("receipt_ids") or []
            if not ids:
                acc["kept_ns_uncited"] += 1  # kept as inferred: no numbers, strict changes nothing
                continue
            if any(i in trunc_ids or i not in receipts for i in ids):
                skipped += 1
                continue
            acc["kept_ns_checked"] += 1
            concl = Conclusion(
                case_id=ep["task_id"],
                top_hypothesis=None,
                ranked=[],
                claims=[Claim(text=c["text"], receipt_ids=ids)],
            )
            _, drops = verify(concl, receipts, drop_uncited_numbers=True, strict=True)
            if drops:
                strict_drop_msgs.append(drops[0])
                new_drops += 1
                if len(acc["examples"]) < 6:
                    acc["examples"].append(drops[0])
        acc["kept_ns_strict_drop"] += new_drops
        acc["kept_ns_skipped_trunc"] += skipped
        pen_old = penalised(budget, len(kept), len(dropped))
        pen_ns = penalised(budget, len(kept_ns), other_drops)
        pen_st = penalised(budget, len(kept_ns) - new_drops, other_drops + new_drops)
        pen_pe = penalised(
            budget, len(kept_ns) - new_drops - skipped, other_drops + new_drops + skipped
        )
        # facts receipt (upper bound): a "number X not in cited receipts" drop whose X is one
        # of the task's facts would pass if the claim also cited task.facts (only the first bad
        # number is recorded, so this can over-count)
        fv = facts_values(ep["task_id"], facts)
        rescued = 0
        for d in [x for x in dropped if not is_summary(x, summary)] + strict_drop_msgs:
            x = drop_number(d)
            if x is not None and any(abs(abs(x) - v) <= 0.01 * max(abs(x), v, 1e-9) for v in fv):
                rescued += 1
        acc["rescued_by_facts"] += rescued
        all_drops = other_drops + new_drops
        pen_fx = penalised(budget, len(kept_ns) - new_drops + rescued, all_drops - rescued)
        if malformed:
            for k in ("reward_old", "reward_nosum", "reward_strict"):
                acc[k] += MALFORMED
            continue
        acc["answered"] += 1
        b = brier(float(r["p"]), float(r["y"]))
        pen_amt = round(1.0 - b - float(r["reward"]), 6)
        acc["pen_old_recorded"] += pen_amt > 1e-6
        acc.setdefault("penalty_sizes", set()).add(pen_amt)
        acc["pen_old"] += pen_old
        acc["pen_nosum"] += pen_ns
        acc["pen_strict"] += pen_st
        acc["pen_strict_pessimistic"] += pen_pe
        acc["pen_facts_ub"] += pen_fx
        size = 0.25
        acc["reward_old"] += float(r["reward"])
        acc["reward_nosum"] += 1.0 - b - (size if pen_ns else 0.0)
        acc["reward_strict"] += 1.0 - b - (size if pen_st else 0.0)
    acc["kind"] = kind or "all"
    n = acc["episodes"]
    for k in ("reward_old", "reward_nosum", "reward_strict"):
        acc[k] /= n
    acc["penalty_sizes"] = sorted(acc.get("penalty_sizes", set()))
    return acc


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    facts: dict[str, dict[str, Any]] = {}
    for line in (ROOT / "evals/m15/manifest.jsonl").open():  # eval + validation only
        t = json.loads(line)
        facts[t["task_id"]] = t["facts"]
    res = [rescore(f, facts, k) for f in FILES for k in (None, "spike_hold", "resolve")]
    (OUT / "rescore_d2.json").write_text(json.dumps(res, indent=2, default=str))
    for a in res:
        n = a["answered"]
        print(
            f"{a['file']:22s} {a['kind']:10s} n={a['episodes']} answered={n} "
            f"pen old {a['pen_old'] / n:.1%} (recorded {a['pen_old_recorded'] / n:.1%}) "
            f"no-summary {a['pen_nosum'] / n:.1%} strict {a['pen_strict'] / n:.1%} "
            f"(pessimistic {a['pen_strict_pessimistic'] / n:.1%}) | kept non-summary "
            f"{a['kept_ns']} checked {a['kept_ns_checked']} strict-drop "
            f"{a['kept_ns_strict_drop']} skipped {a['kept_ns_skipped_trunc']} | "
            f"reward {a['reward_old']:.4f} -> {a['reward_nosum']:.4f} -> {a['reward_strict']:.4f}"
            f" | facts-rescued drops {a['rescued_by_facts']} -> pen <= {a['pen_facts_ub'] / n:.1%}"
            f" | trunc results {a['truncated_results']} | sizes {a['penalty_sizes']}"
        )
        if a["kind"] == "all":
            for e in a["examples"]:
                print("    ", e)


if __name__ == "__main__":
    main()
