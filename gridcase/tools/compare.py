"""kpi.compare: the KPI over the window vs the same hours on previous days."""

from __future__ import annotations

import math
from typing import Any

from gridcase.semantic.kpis import Filters, KpiQuery, Limit
from gridcase.tools.base import ToolCtx, tool
from gridcase.tools.registry import KpiWindow


def kpi_total(
    ctx: ToolCtx, kpi: str, filters: Filters, window: Any, limits: list[Limit] | None = None
) -> float | None:
    q = KpiQuery(
        kpi=kpi,
        grain="total",
        window=window,
        as_of=ctx.as_of,
        filters=filters,
        limits=limits or [],
    )
    rows = ctx.kpi(q)
    v = rows[0][-1] if rows else None
    return None if v is None else float(v)


@tool("kpi.compare")
def kpi_compare(ctx: ToolCtx, inputs: KpiWindow) -> tuple[dict[str, Any], float | None]:
    now = kpi_total(ctx, inputs.kpi, inputs.filters, inputs.window, inputs.limits) or 0.0
    past = [
        v
        for w in ctx.baseline_windows(inputs.window, inputs.baseline_days)
        if (v := kpi_total(ctx, inputs.kpi, inputs.filters, w, inputs.limits)) is not None
    ]
    expected = sum(past) / len(past) if past else None
    change = None if expected is None else now - expected
    ratio = now / expected if expected else None
    sd = (
        math.sqrt(sum((x - expected) ** 2 for x in past) / len(past))
        if expected is not None and len(past) > 1
        else None
    )
    direction = (
        None if change is None else ("up" if change > 0 else "down" if change < 0 else "flat")
    )
    output: dict[str, Any] = {
        "kpi": inputs.kpi,
        "filters": inputs.filters,
        **({"limits": [lim.model_dump() for lim in inputs.limits]} if inputs.limits else {}),
        "now": now,
        "expected": expected,
        "change": change,
        "abs_change": None if change is None else abs(change),
        "ratio": ratio,
        "pct_change": None if ratio is None else 100 * (ratio - 1),
        "direction": direction,
        "typical_spread": sd,
        "baseline_days_used": len(past),
        "lowest_past": min(past) if past else None,
        "highest_past": max(past) if past else None,
        "insufficient": not past,
    }
    return output, output["pct_change"]
