"""anomaly.detect: robust z-score of a metric against baseline.expected; flag when |z| > 3
for at least two consecutive hours."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from gridcase.semantic.compiler import compile_query
from gridcase.semantic.schema import MetricQuery
from gridcase.tools.base import ToolCtx, ToolError, knowable_horizon, knowable_hours, tool
from gridcase.tools.baseline import fit_baseline, predict
from gridcase.tools.registry import MetricEntityWindow

Z_LIMIT = 3.0
MIN_RUN = 2


@tool("anomaly.detect")
def anomaly_detect(ctx: ToolCtx, inputs: MetricEntityWindow) -> tuple[dict[str, Any], float | None]:
    fit, _ = fit_baseline(ctx, inputs.metric, inputs.entity)
    expected = {h["t"]: h for h in predict(ctx, fit, inputs.entity, inputs.window, inputs.metric)}
    actual = {
        t: float(v)
        for t, v in ctx.metric(inputs.metric, inputs.entity, inputs.window)
        if v is not None
    }
    if not actual:
        raise ToolError(f"no {inputs.metric} data for {inputs.entity} in the window")
    hours: list[dict[str, Any]] = []
    for t in sorted(expected):
        if t not in actual:
            continue
        e = expected[t]
        z = (actual[t] - e["expected"]) / e["sigma"]
        hours.append({"t": t, "actual": actual[t], "expected": e["expected"], "z": z})
    runs = _runs(hours)
    flagged_ts = [t for run in runs for t in run]
    max_abs_z = max((abs(h["z"]) for h in hours), default=0.0)
    worst = max(hours, key=lambda h: abs(h["z"]), default=None)
    # The event is the cluster of flagged runs (gaps under MERGE_GAP hours) that holds the
    # worst hour; without a flagged run it is the worst hour alone.
    event_start: datetime | None = None
    event_end: datetime | None = None
    if worst is not None:
        cluster = _cluster_with(runs, worst["t"])
        event_start, event_end = cluster[0], cluster[-1] + timedelta(hours=1)
    train_max = max(abs(a) for a in actual.values()) if actual else 0.0
    at_limit = sum(1 for a in actual.values() if train_max and abs(a) >= 0.95 * train_max)
    first: datetime | None = flagged_ts[0] if flagged_ts else None
    series = set(
        compile_query(
            MetricQuery(
                metric=inputs.metric, entity=inputs.entity, window=inputs.window, as_of=ctx.as_of
            )
        ).series_used
    )
    horizon = min(inputs.window[1], knowable_horizon(series, ctx.as_of))
    # Evidence is insufficient when the series cannot yet cover the event (lagged
    # publication) or fewer than half the knowable hours are present.
    insufficient = (
        inputs.event_start is not None and horizon <= inputs.event_start + timedelta(hours=1)
    ) or (len(actual) < 0.5 * max(1, knowable_hours(inputs.window, series, ctx.as_of)))
    return {
        "metric": inputs.metric,
        "entity": inputs.entity,
        "insufficient": insufficient,
        "knowable_until": horizon,
        "event_start": event_start,
        "event_end": event_end,
        "flagged": bool(flagged_ts),
        "max_abs_z": max_abs_z,
        "max_abs_dev": abs(worst["actual"] - worst["expected"]) if worst else 0.0,
        "worst_dev": (worst["actual"] - worst["expected"]) if worst else 0.0,
        "worst_t": worst["t"] if worst else None,
        "n_flagged_hours": len(flagged_ts),
        "first_flag_t": first,
        "missing_hours": max(0, knowable_hours(inputs.window, series, ctx.as_of) - len(actual)),
        "share_at_limit": at_limit / len(actual) if actual else 0.0,
        "hours": hours,
    }, max_abs_z


MERGE_GAP = timedelta(hours=6)


def _cluster_with(runs: list[list[datetime]], t: datetime) -> list[datetime]:
    """Merge runs separated by less than MERGE_GAP; return the cluster containing t (or the
    one nearest to t when t itself is not flagged), else [t]."""
    clusters: list[list[datetime]] = []
    for run in runs:
        if clusters and run[0] - clusters[-1][-1] < MERGE_GAP:
            clusters[-1] = clusters[-1] + run
        else:
            clusters.append(list(run))
    for c in clusters:
        if c[0] <= t <= c[-1]:
            return c
    return [t]


def _runs(hours: list[dict[str, Any]]) -> list[list[datetime]]:
    """Runs of >= MIN_RUN consecutive hours with |z| > Z_LIMIT, in time order."""
    out: list[list[datetime]] = []
    run: list[datetime] = []
    for h in hours:
        if abs(h["z"]) > Z_LIMIT:
            run.append(h["t"])
        else:
            if len(run) >= MIN_RUN:
                out.append(run)
            run = []
    if len(run) >= MIN_RUN:
        out.append(run)
    return out
