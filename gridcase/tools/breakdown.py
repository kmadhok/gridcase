"""by.market / by.category / by.size / by.trader / by.odds: which segment the change came from.

Each segment's contribution is its own change against the same hours on previous days. For
volumes and counts that is simply now − expected (the contributions of volume and trades add
up to the total change exactly). For averages and shares (average trade size, returning-trader
share) a segment contributes weight × value, where the weight is its share of the KPI's
denominator (trades, active traders): so a change can come from more of a segment (mix) or
from the segment itself changing (rate), and the contributions add up approximately.
`top_share` is the part of the total change the leading segment accounts for.
"""

from __future__ import annotations

from typing import Any

from gridcase.semantic.kpis import Derived, KpiQuery, load_kpis
from gridcase.tools.base import ToolCtx, ToolError, tool
from gridcase.tools.compare import kpi_total
from gridcase.tools.registry import BREAKDOWNS, Breakdown

Window = Any


def _by_segment(
    ctx: ToolCtx, kpi: str, dim: str, filters: dict[str, str], w: Window
) -> dict[str, float]:
    q = KpiQuery(kpi=kpi, grain="total", window=w, as_of=ctx.as_of, filters=filters, group_by=dim)
    return {str(seg): float(v) for seg, v in ctx.kpi(q) if v is not None}


def _contributions(ctx: ToolCtx, inputs: Breakdown, dim: str, w: Window) -> dict[str, float]:
    """Per segment, the part of the KPI it carries in window w."""
    kpis, _ = load_kpis()
    if inputs.kpi not in kpis:
        raise ToolError(f"unknown KPI {inputs.kpi!r}")
    kpi = kpis[inputs.kpi]
    values = _by_segment(ctx, inputs.kpi, dim, inputs.filters, w)
    if not isinstance(kpi, Derived):
        return values
    weights = _by_segment(ctx, kpi.weight, dim, inputs.filters, w)
    total = sum(weights.values())
    return {s: (weights.get(s, 0.0) / total) * v for s, v in values.items()} if total else {}


def breakdown(ctx: ToolCtx, inputs: Breakdown, dim: str) -> tuple[dict[str, Any], float | None]:
    windows = ctx.baseline_windows(inputs.window, inputs.baseline_days)
    now_c = _contributions(ctx, inputs, dim, inputs.window)
    past = [_contributions(ctx, inputs, dim, w) for w in windows]
    segments = sorted(set(now_c) | {s for p in past for s in p})
    expected = {s: sum(p.get(s, 0.0) for p in past) / len(past) for s in segments} if past else {}
    now_total = kpi_total(ctx, inputs.kpi, inputs.filters, inputs.window) or 0.0
    past_totals = [kpi_total(ctx, inputs.kpi, inputs.filters, w) or 0.0 for w in windows]
    total_change = now_total - sum(past_totals) / len(past_totals) if past_totals else None

    rows: list[dict[str, Any]] = []
    for s in segments:
        change = now_c.get(s, 0.0) - expected.get(s, 0.0)
        share = change / total_change if total_change else None
        rows.append(
            {
                "segment": s,
                "now": now_c.get(s, 0.0),
                "expected": expected.get(s, 0.0),
                "change": change,
                "share_of_change": share,
            }
        )
    rows.sort(key=lambda r: -(r["share_of_change"] or 0.0) if total_change else -abs(r["change"]))
    top = rows[0] if rows and total_change else None
    shown = rows[: inputs.top_n]
    rest = rows[inputs.top_n :]
    if rest:
        shown.append(
            {
                "segment": f"{len(rest)} others",
                "now": sum(r["now"] for r in rest),
                "expected": sum(r["expected"] for r in rest),
                "change": sum(r["change"] for r in rest),
                "share_of_change": (sum(r["change"] for r in rest) / total_change)
                if total_change
                else None,
            }
        )
    output: dict[str, Any] = {
        "kpi": inputs.kpi,
        "dimension": dim,
        "filters": inputs.filters,
        "total_change": total_change,
        "direction": None if not total_change else ("up" if total_change > 0 else "down"),
        "top_segment": top["segment"] if top else None,
        "top_share": top["share_of_change"] if top else None,
        "top_change": abs(top["change"]) if top else None,
        "second_share": rows[1]["share_of_change"] if top and len(rows) > 1 else None,
        "n_segments": len(segments),
        "segments": shown,
        "baseline_days_used": len(windows),
        "insufficient": not windows,
    }
    return output, output["top_share"]


def _register(name: str, dim: str) -> None:
    @tool(name)
    def run(ctx: ToolCtx, inputs: Breakdown) -> tuple[dict[str, Any], float | None]:
        return breakdown(ctx, inputs, dim)


for _name, _dim in BREAKDOWNS.items():
    _register(_name, _dim)
