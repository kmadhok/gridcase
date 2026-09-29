"""contribution.subregion: which subregion the miss concentrates in.

EIA publishes no sub-BA forecast, so the miss is proxied against the same hours a week
earlier. Two views are reported:

* `level_shares`: each subregion's share of the change in mean demand (additive; weather moves
  every subregion, so this is confounded by weather).
* `shares` (the governed one): each subregion's share of the *redistribution* of load, i.e.
  positive change in its share of BA demand times BA demand. Weather leaves shares roughly
  alone; a local outage, large customer or zonal forecast error moves them. Below a
  materiality floor (2% of BA demand) there is no concentration to speak of and every share
  is 0.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from gridcase.tools.base import ToolCtx, ToolError, knowable_horizon, tool
from gridcase.tools.registry import EntityWindow

MATERIALITY = 0.02


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
    if inputs.event_start is not None and start < inputs.event_start < end:
        start = inputs.event_start  # compare the event hours, not the whole window
    week = timedelta(days=7)
    now = _means(ctx.metric("demand_by_subregion", inputs.entity, (start, end)))
    prev = _means(ctx.metric("demand_by_subregion", inputs.entity, (start - week, end - week)))
    horizon = knowable_horizon({"eia.subba_demand"}, ctx.as_of)
    if not now and start >= horizon:
        # Sub-BA demand lags 36 h: the event hours are not knowable yet. Unknown, not refuted.
        return {"insufficient": True, "knowable_until": horizon, "shares": {}}, None
    if not now or not prev:
        raise ToolError(f"no subregion demand for {inputs.entity} in window or the week before")
    subs = sorted(set(now) & set(prev))
    total_now, total_prev = sum(now[s] for s in subs), sum(prev[s] for s in subs)
    delta = {s: now[s] - prev[s] for s in subs}
    total = sum(delta.values())
    level_shares = {s: (delta[s] / total if total else 0.0) for s in subs}

    redistribution = {
        s: (now[s] / total_now - prev[s] / total_prev) * total_now
        if total_now and total_prev
        else 0.0
        for s in subs
    }
    positive = {s: max(0.0, v) for s, v in redistribution.items()}
    moved = sum(positive.values())
    material = moved >= MATERIALITY * total_now
    shares = {s: (positive[s] / moved if material and moved else 0.0) for s in subs}
    top = max(subs, key=lambda s: (shares[s], s))
    return {
        "entity": inputs.entity,
        "window_start": start,
        "total_change_mw": total,
        "delta_mw": delta,
        "level_shares": level_shares,
        "redistribution_mw": redistribution,
        "moved_mw": moved,
        "material": material,
        "shares": shares,
        "top_subregion": top,
        "top_share": shares[top],
        "top_delta_mw": redistribution[top],
        "basis": "share of BA load redistributed vs the same hours a week earlier",
    }, shares[top]
