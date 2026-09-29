"""Scoring: root-cause top-1/top-3, false-alarm rate, Brier, receipt coverage, trajectory
checks, cost and latency; per hypothesis and per region; a confusion matrix."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from gridcase.bench.run import CaseResult, SuiteResult


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _block(cases: list[CaseResult]) -> dict[str, Any]:
    n = len(cases)
    out: dict[str, Any] = {"n": n, "failures": sum(1 for c in cases if c.error)}
    if n == 0:
        out.update({"top1": None, "top3": None, "brier": None, "receipt_coverage": None})
        out.update({"per_hypothesis": {}, "per_region": {}, "confusion": {}, "trajectory": {}})
        out.update({"cost_usd_per_case": None, "latency_s_per_case": None})
        return out
    out["top1"] = sum(c.top1 for c in cases) / n
    out["top3"] = sum(c.top3 for c in cases) / n
    out["brier"] = _mean([c.brier for c in cases])
    # coverage is a property of conclusions; a case that produced none is a failure, not a
    # claim without a receipt
    out["receipt_coverage"] = _mean([c.receipt_coverage for c in cases if not c.error])
    by_h: dict[str, list[CaseResult]] = defaultdict(list)
    by_r: dict[str, list[CaseResult]] = defaultdict(list)
    for c in cases:
        by_h[c.generator or (c.ground_truth[0] if c.ground_truth else "none")].append(c)
        by_r[c.entity].append(c)
    out["per_hypothesis"] = {
        h: {
            "n": len(v),
            "top1": sum(c.top1 for c in v) / len(v),
            "top3": sum(c.top3 for c in v) / len(v),
        }
        for h, v in sorted(by_h.items())
    }
    out["per_region"] = {
        r: {
            "n": len(v),
            "top1": sum(c.top1 for c in v) / len(v),
            "top3": sum(c.top3 for c in v) / len(v),
        }
        for r, v in sorted(by_r.items())
    }
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in cases:
        truth = c.ground_truth[0] if c.ground_truth else "none"
        pred = c.predicted[0] if c.predicted else "none"
        confusion[truth][pred] += 1
    out["confusion"] = {k: dict(v) for k, v in confusion.items()}
    out["trajectory"] = {
        "h0_first_rate": sum(c.plan_h0_first for c in cases) / n,
        "two_plus_hypotheses_rate": sum(c.n_hypotheses >= 2 for c in cases) / n,
        "mean_steps": _mean([float(c.steps_run) for c in cases]),
        "mean_skipped": _mean([float(c.steps_skipped) for c in cases]),
        "mean_llm_calls": _mean([float(c.llm_calls) for c in cases]),
    }
    out["cost_usd_per_case"] = _mean([c.cost_usd for c in cases])
    out["latency_s_per_case"] = _mean([c.latency_s for c in cases])
    return out


def score(suite: SuiteResult) -> dict[str, Any]:
    null_n = len(suite.null)
    return {
        "synthetic": _block(suite.synthetic),
        "replay": _block(suite.replay),
        "null": {
            "n": null_n,
            "false_alarm_rate": (sum(c.flagged for c in suite.null) / null_n) if null_n else None,
            "failures": sum(1 for c in suite.null if c.error),
        },
    }
