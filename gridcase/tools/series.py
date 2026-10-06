"""kpi.series: the Explore chat's query, run through the semantic layer. Any public KPI, any
fixed-segment filters, an optional group-by (top segments by value), at a grain, over a window
resolved at as_of. Run by the chat only; not offered to the investigation planner."""

from __future__ import annotations

from typing import Any

from gridcase.explore.query import ExploreQuery, QueryError, resolve_window
from gridcase.semantic.kpis import KpiQuery, load_kpis
from gridcase.tools.base import ToolCtx, ToolError, tool


@tool("kpi.series")
def kpi_series(ctx: ToolCtx, inputs: ExploreQuery) -> tuple[dict[str, Any], float | None]:
    try:
        lo, hi = resolve_window(inputs, ctx.as_of, ctx.record_start())
    except QueryError as e:
        raise ToolError(str(e)) from e
    kpi = load_kpis()[0][inputs.kpi]
    timed, grouped = inputs.grain != "total", inputs.group_by is not None
    q = KpiQuery(
        kpi=inputs.kpi,
        grain=inputs.grain,
        window=(lo, hi),
        as_of=ctx.as_of,
        filters=dict(inputs.filter_map()),
        group_by=inputs.group_by,
    )
    rows: list[dict[str, Any]] = []
    for raw in ctx.kpi(q):
        t = raw[0] if timed else None
        seg = raw[1 if timed else 0] if grouped else None
        v = raw[-1]
        rows.append(
            {
                "t": t.isoformat() if t is not None else None,
                "segment": None if seg is None else str(seg),
                "value": None if v is None else float(v),
            }
        )

    out: dict[str, Any] = {
        "kpi": inputs.kpi,
        "label": kpi.label,
        "unit": kpi.unit,
        "filters": inputs.filter_map(),
        "group_by": inputs.group_by,
        "grain": inputs.grain,
        "window": {"name": inputs.window, "start": lo.isoformat(), "end": hi.isoformat()},
        "gap_overlap": ctx.gap_overlaps(lo, hi),
    }
    if grouped:
        totals: dict[str, float] = {}
        for r in rows:
            totals[r["segment"]] = totals.get(r["segment"], 0.0) + (r["value"] or 0.0)
        ranked = sorted(totals, key=lambda s: (-totals[s], s))
        rank = {s: i for i, s in enumerate(ranked[: inputs.top_n])}
        rows = sorted(
            (r for r in rows if r["segment"] in rank),
            key=lambda r: (r["t"] or "", rank[r["segment"]]),
        )
        out["segments_total"] = len(ranked)
    elif timed:
        valued = [r for r in rows if r["value"] is not None]
        if valued:
            peak = max(valued, key=lambda r: r["value"])
            low = min(valued, key=lambda r: r["value"])
            out["peak"] = {"t": peak["t"], "value": peak["value"]}
            out["low"] = {"t": low["t"], "value": low["value"]}
    out["rows"] = rows
    return out, None
