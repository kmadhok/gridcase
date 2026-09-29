"""contribution.subregion: share of the change in demand vs the preceding week, by subregion.

EIA publishes no sub-BA forecast, so the "miss" is proxied by the change in each subregion's
mean demand between the window and the seven days before it. Shares are additive by
construction (they sum to 1 when the total change is non-zero).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from gridcase.tools.base import ToolCtx, ToolError, tool
from gridcase.tools.registry import EntityWindow


def _means(rows: list[tuple[Any, ...]]) -> dict[str, float]:
    acc: dict[str, list[float]] = defaultdict(list)
    for _, sub, v in rows:
        if v is not None:
            acc[str(sub)].append(float(v))
    return {s: sum(v) / len(v) for s, v in acc.items()}


@tool("contribution.subregion")
def contribution_subregion(
    ctx: ToolCtx, inputs: EntityWindow
) -> tuple[dict[str, Any], float | None]:
    start, end = inputs.window
    now = _means(ctx.metric("demand_by_subregion", inputs.entity, inputs.window))
    prev = _means(
        ctx.metric("demand_by_subregion", inputs.entity, (start - timedelta(days=7), start))
    )
    if not now or not prev:
        raise ToolError(f"no subregion demand for {inputs.entity} in window or the week before")
    subs = sorted(set(now) & set(prev))
    delta = {s: now[s] - prev[s] for s in subs}
    total = sum(delta.values())
    if total != 0:
        shares = {s: delta[s] / total for s in subs}
    else:
        abs_total = sum(abs(d) for d in delta.values())
        shares = {s: (abs(delta[s]) / abs_total if abs_total else 0.0) for s in subs}
    top = max(subs, key=lambda s: (abs(shares[s]), s))
    return {
        "entity": inputs.entity,
        "total_change_mw": total,
        "delta_mw": delta,
        "shares": shares,
        "top_subregion": top,
        "top_share": abs(shares[top]),
        "basis": "change vs preceding 7 days"
        if total != 0
        else "abs change (total change is zero)",
    }, abs(shares[top])
