"""kpi.series: the Explore chat's query, run through the semantic layer. Any public KPI, any
fixed-segment filters, an optional group-by (top segments by value), at a grain, over a window
resolved at as_of. Run by the chat only; not offered to the investigation planner."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from gridcase.explore.query import ExploreQuery, QueryError, resolve_window
from gridcase.semantic.kpis import KpiQuery, load_kpis
from gridcase.tools.base import ToolCtx, ToolError, tool


def span(lo: datetime, hi: datetime) -> list[str]:
    """How long the window is, as an answer may say it ("7 days", "168 hours"). The verifier
    treats these phrases as labels when this receipt is cited, so naming the period is not
    mistaken for stating a number, and only the period this evidence really covers passes."""
    hours = int((hi - lo).total_seconds() // 3600)
    out = [f"{hours // 24} day{'s' if hours != 24 else ''}"] if hours % 24 == 0 else []
    return [*out, f"{hours} hour{'s' if hours != 1 else ''}"]


MATERIAL_GAP = 0.01  # a gap is worth a caveat once it is 1% of the window


def collector_gap_minutes(ctx: ToolCtx, lo: datetime, hi: datetime) -> float:
    """Minutes of the window our collector missed, from gaps known at as_of."""
    row = ctx.raw(
        "SELECT coalesce(sum(date_diff('second', greatest(gap_from, ?), least(gap_to, ?))), 0) "
        "FROM collector_gaps WHERE gap_to > ? AND gap_from < ? "
        "AND coalesce(detected_at, gap_to) <= ?",
        [lo, hi, lo, hi, ctx.as_of],
    )
    return round(float(row[0][0]) / 60, 1)


def warmup_fields(kpi: str, lo: datetime, start: datetime | None) -> dict[str, Any]:
    """For a KPI the watch warms up (new traders), when the window starts inside the record's
    first days: the numbers an answer needs to explain why, as evidence."""
    from gridcase.market_alarm import load_watch  # the watch's own warm-up, kept in one place

    warmup = load_watch()[1].warmup_days.get(kpi, 0)
    if not warmup or start is None or lo >= start + timedelta(days=warmup):
        return {}
    return {"warmup_days": warmup, "record_start": start.isoformat()}


def caveats(lo: datetime, hi: datetime, gap_minutes: float, warm: dict[str, Any]) -> list[str]:
    """What an answer must say alongside the number, decided by rule, not by the model."""
    out = []
    if gap_minutes >= MATERIAL_GAP * (hi - lo).total_seconds() / 60:
        out.append(
            f"Our collector missed {gap_minutes:g} minutes of trades in this period, "
            "so the number may be a little low."
        )
    if warm:
        warmup, start = warm["warmup_days"], datetime.fromisoformat(warm["record_start"])
        out.append(
            f"'New' means new to our record, which began on {start:%Y-%m-%d}; in its first "
            f"{warmup} days, wallets that traded before then still count as new, so these "
            "numbers run high."
        )
    return out


@tool("kpi.series")
def kpi_series(ctx: ToolCtx, inputs: ExploreQuery) -> tuple[dict[str, Any], float | None]:
    try:
        start = ctx.record_start()
        lo, hi = resolve_window(inputs, ctx.as_of, start)
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

    gap_minutes = collector_gap_minutes(ctx, lo, hi)  # after the KPI query: its SQL comes first
    warm = warmup_fields(inputs.kpi, lo, start)
    out: dict[str, Any] = {
        "kpi": inputs.kpi,
        "label": kpi.label,
        "unit": kpi.unit,
        "filters": inputs.filter_map(),
        "group_by": inputs.group_by,
        "grain": inputs.grain,
        "window": {
            "name": inputs.window,
            "start": lo.isoformat(),
            "end": hi.isoformat(),
            "span": span(lo, hi),
        },
        "collector_gap_minutes": gap_minutes,
        "caveats": caveats(lo, hi, gap_minutes, warm),
        **warm,
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
