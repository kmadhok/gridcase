"""data.coverage: did we actually see the trading in the window? Collector gaps, hours with no
trades at all, and how stale our newest data was at as_of."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gridcase.tools.base import ToolCtx, tool
from gridcase.tools.registry import CoverageInputs


@tool("data.coverage")
def data_coverage(ctx: ToolCtx, inputs: CoverageInputs) -> tuple[dict[str, Any], float | None]:
    lo, hi = inputs.window
    gaps = ctx.raw(
        "SELECT gap_from, gap_to FROM collector_gaps WHERE gap_to > ? AND gap_from < ? "
        "AND coalesce(detected_at, gap_to) <= ?",
        [lo, hi, ctx.as_of],
    )
    gap_minutes = sum(
        (min(b, hi) - max(a, lo)).total_seconds() / 60 for a, b in gaps if min(b, hi) > max(a, lo)
    )
    hours = ctx.raw(
        "SELECT date_trunc('hour', ts) AS h, count(*) FROM trades_as_of(?) "
        "WHERE ts >= ? AND ts < ? GROUP BY 1",
        [ctx.as_of, lo, hi],
    )
    seen = {h for h, _ in hours}
    start = ctx.record_start()
    expected_hours = []
    h = lo.replace(minute=0, second=0, microsecond=0)
    while h < min(hi, ctx.as_of):
        if start is None or h >= start:
            expected_hours.append(h)
        h += timedelta(hours=1)
    empty = [h for h in expected_hours if h not in seen]
    newest = ctx.data_version()
    stale = (ctx.as_of - newest).total_seconds() / 60 if newest else None
    complete = gap_minutes == 0 and not empty and stale is not None and stale < 30
    output = {
        "gap_minutes": round(gap_minutes, 1),
        "gaps": len(gaps),
        "empty_hours": len(empty),
        "hours_checked": len(expected_hours),
        "freshness_minutes": None if stale is None else round(stale, 1),
        "complete": 1 if complete else 0,
        "before_record_start": 1 if start is not None and lo < start else 0,
    }
    return output, gap_minutes
