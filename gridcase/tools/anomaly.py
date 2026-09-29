"""anomaly.detect: robust z-score of a metric against baseline.expected; flag when |z| > 3
for at least two consecutive hours."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from gridcase.tools.base import ToolCtx, expected_hours, tool
from gridcase.tools.baseline import fit_baseline, predict
from gridcase.tools.registry import MetricEntityWindow

Z_LIMIT = 3.0
MIN_RUN = 2


@tool("anomaly.detect")
def anomaly_detect(ctx: ToolCtx, inputs: MetricEntityWindow) -> tuple[dict[str, Any], float | None]:
    fit, _ = fit_baseline(ctx, inputs.metric, inputs.entity)
    expected = {h["t"]: h for h in predict(ctx, fit, inputs.entity, inputs.window)}
    actual = {
        t: float(v)
        for t, v in ctx.metric(inputs.metric, inputs.entity, inputs.window)
        if v is not None
    }
    hours: list[dict[str, Any]] = []
    for t in sorted(expected):
        if t not in actual:
            continue
        e = expected[t]
        z = (actual[t] - e["expected"]) / e["sigma"]
        hours.append({"t": t, "actual": actual[t], "expected": e["expected"], "z": z})
    flagged_ts = _runs(hours)
    max_abs_z = max((abs(h["z"]) for h in hours), default=0.0)
    train_max = max(abs(a) for a in actual.values()) if actual else 0.0
    at_limit = sum(1 for a in actual.values() if train_max and abs(a) >= 0.95 * train_max)
    first: datetime | None = flagged_ts[0] if flagged_ts else None
    return {
        "metric": inputs.metric,
        "entity": inputs.entity,
        "flagged": bool(flagged_ts),
        "max_abs_z": max_abs_z,
        "n_flagged_hours": len(flagged_ts),
        "first_flag_t": first,
        "missing_hours": expected_hours(inputs.window) - len(actual),
        "share_at_limit": at_limit / len(actual) if actual else 0.0,
        "hours": hours,
    }, max_abs_z


def _runs(hours: list[dict[str, Any]]) -> list[datetime]:
    """Timestamps in runs of >= MIN_RUN consecutive hours with |z| > Z_LIMIT."""
    out: list[datetime] = []
    run: list[datetime] = []
    for h in hours:
        if abs(h["z"]) > Z_LIMIT:
            run.append(h["t"])
        else:
            if len(run) >= MIN_RUN:
                out += run
            run = []
    if len(run) >= MIN_RUN:
        out += run
    return out
