"""The daily digest's checks: one per standing question, each a pure function of (inputs,
as_of) that reads trades only through governed KPIs (ctx.kpi) and returns chart-ready numbers.
The digest (gridcase/digest.py) writes the sentences from these outputs."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from gridcase.semantic.kpis import Grain, KpiQuery
from gridcase.tools.base import HOUR, Rows, ToolCtx, tool
from gridcase.tools.compare import kpi_total
from gridcase.tools.registry import (
    ByDayInputs,
    MoversInputs,
    PeriodInputs,
    ShareShiftInputs,
    SwingsInputs,
    TotalsInputs,
    Window,
)

DAY = timedelta(days=1)
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
Out = tuple[dict[str, Any], float | None]


def _rows(
    ctx: ToolCtx, kpi: str, window: Window, grain: Grain = "total", group_by: str | None = None
) -> Rows:
    return ctx.kpi(
        KpiQuery(kpi=kpi, grain=grain, window=window, as_of=ctx.as_of, group_by=group_by)
    )


def _by_segment(ctx: ToolCtx, kpi: str, window: Window, dim: str) -> dict[str, float]:
    return {seg: float(v or 0.0) for seg, v in _rows(ctx, kpi, window, group_by=dim)}


@tool("digest.totals")
def totals(ctx: ToolCtx, inputs: TotalsInputs) -> Out:
    items = []
    for k in inputs.kpis:
        now = kpi_total(ctx, k, {}, inputs.current) or 0.0
        before = kpi_total(ctx, k, {}, inputs.previous) if inputs.previous else None
        items.append(
            {
                "kpi": k,
                "now": now,
                "before": before,
                "pct_change": 100 * (now / before - 1) if before else None,
            }
        )
    days = (inputs.current[1] - inputs.current[0]).days
    return {"days": days, "items": items, "insufficient": inputs.previous is None}, None


@tool("digest.share_shift")
def share_shift(ctx: ToolCtx, inputs: ShareShiftInputs) -> Out:
    now = _by_segment(ctx, "volume_share", inputs.current, inputs.dimension)
    before = (
        _by_segment(ctx, "volume_share", inputs.previous, inputs.dimension)
        if inputs.previous
        else {}
    )
    segments: list[dict[str, Any]] = [
        {
            "segment": s,
            "now": now.get(s, 0.0),
            "before": before.get(s, 0.0) if inputs.previous else None,
            "change": now.get(s, 0.0) - before.get(s, 0.0) if inputs.previous else None,
        }
        for s in sorted(set(now) | set(before))
    ]
    segments.sort(key=lambda x: (-x["now"], x["segment"]))
    out: dict[str, Any] = {
        "dimension": inputs.dimension,
        "segments": segments,
        "insufficient": inputs.previous is None,
    }
    if inputs.previous and segments:
        gainer = max(segments, key=lambda x: (x["change"], x["segment"]))
        loser = min(segments, key=lambda x: (x["change"], x["segment"]))
        out |= {
            "top_gainer": gainer["segment"],
            "gain": gainer["change"],
            "top_loser": loser["segment"],
            "loss": loser["change"],
        }
    return out, out.get("gain")


@tool("digest.movers")
def movers(ctx: ToolCtx, inputs: MoversInputs) -> Out:
    """Each market's volume in the window (a day) against its daily average over the previous
    days our record fully covers; markets ranked by the dollar change."""
    base = ctx.baseline_windows(inputs.window, inputs.baseline_days)
    if not base:
        return {"markets": [], "baseline_days_used": 0, "insufficient": True}, None
    now = _by_segment(ctx, "volume_usd", inputs.window, "market")
    starts = {lo for lo, _ in base}
    span = (min(starts), max(hi for _, hi in base))
    past: dict[str, float] = defaultdict(float)
    for t, seg, v in _rows(ctx, "volume_usd", span, grain="day", group_by="market"):
        if t in starts:
            past[seg] += float(v or 0.0)
    items: list[dict[str, Any]] = []
    for m in set(now) | set(past):
        usual = past.get(m, 0.0) / len(base)
        v = now.get(m, 0.0)
        items.append(
            {
                "market": m,
                "now": v,
                "usual": usual,
                "change": v - usual,
                "ratio": v / usual if usual else None,
            }
        )
    items.sort(key=lambda x: (-x["change"], x["market"]))
    top = items[: inputs.top_n]
    out: dict[str, Any] = {"markets": top, "baseline_days_used": len(base), "insufficient": False}
    return out, top[0]["change"] if top else None


@tool("digest.odds_swings")
def odds_swings(ctx: ToolCtx, inputs: SwingsInputs) -> Out:
    """Outcomes whose price moved most from the first to the last trade of the window (event
    markets, 20+ trades); a market's mirror outcome is not listed twice."""

    def prices(kpi: str) -> dict[str, float]:  # None: too few trades to count
        rows = _rows(ctx, kpi, inputs.window, group_by="outcome")
        return {seg: float(v) for seg, v in rows if v is not None}

    opening, closing = prices("opening_price"), prices("closing_price")
    considered = [s for s in opening if s in closing]
    # biggest move first; of a market's mirror outcomes, name the one that rose
    considered.sort(
        key=lambda s: (-round(abs(closing[s] - opening[s]), 6), closing[s] < opening[s], s)
    )
    swings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for s in considered:
        market, _, outcome = s.rpartition(" · ")
        if market in seen:
            continue
        seen.add(market)
        swings.append(
            {
                "market": market,
                "outcome": outcome,
                "open": opening[s],
                "close": closing[s],
                "move": closing[s] - opening[s],
            }
        )
        if len(swings) == inputs.top_n:
            break
    out: dict[str, Any] = {"swings": swings, "outcomes_considered": len(considered)}
    return out, abs(swings[0]["move"]) if swings else None


@tool("digest.by_day")
def by_day(ctx: ToolCtx, inputs: ByDayInputs) -> Out:
    days = [
        {"day": t.date().isoformat(), "value": float(v)}
        for t, v in _rows(ctx, inputs.kpi, inputs.window, grain="day")
        if v is not None
    ]
    out: dict[str, Any] = {"kpi": inputs.kpi, "days": days}
    if days:
        peak = max(days, key=lambda d: (d["value"], d["day"]))
        low = min(days, key=lambda d: (d["value"], d["day"]))
        out |= {
            "mean": sum(d["value"] for d in days) / len(days),
            "peak_day": peak["day"],
            "peak_value": peak["value"],
            "low_day": low["day"],
            "low_value": low["value"],
        }
    return out, out.get("peak_value")


@tool("digest.retention")
def retention(ctx: ToolCtx, inputs: PeriodInputs) -> Out:
    """For each day's new traders (first trade in our record that day), how many traded again
    the next day. The window's last day is left out: its newcomers are judged tomorrow."""
    counts: dict[tuple[str, str], int] = {}
    for t, seg, v in _rows(
        ctx, "active_traders", inputs.window, grain="day", group_by="first_seen_day"
    ):
        counts[(t.date().isoformat(), seg)] = int(v)
    days: list[dict[str, Any]] = []
    d = inputs.window[0]
    while d + 2 * DAY <= inputs.window[1]:
        key, nxt = d.date().isoformat(), (d + DAY).date().isoformat()
        new = counts.get((key, key), 0)
        if new:
            back = counts.get((nxt, key), 0)
            days.append({"day": key, "new": new, "came_back": back, "rate": back / new})
        d += DAY
    out: dict[str, Any] = {"days": days}
    if days:
        out |= {
            "latest": days[-1],
            "mean_rate": sum(x["came_back"] for x in days) / sum(x["new"] for x in days),
        }
    return out, days[-1]["rate"] if days else None


@tool("digest.rhythm")
def rhythm(ctx: ToolCtx, inputs: PeriodInputs) -> Out:
    """Average volume in each weekday x hour (UTC) over the window, counting only hours our
    record covers (an hour with no trades counts as zero)."""
    start = max(inputs.window[0], ctx.record_start() or inputs.window[0])
    slots: dict[tuple[int, int], int] = defaultdict(int)
    h = start
    while h < inputs.window[1]:
        slots[(h.weekday(), h.hour)] += 1
        h += HOUR
    sums: dict[tuple[int, int], float] = defaultdict(float)
    for t, v in _rows(ctx, "volume_usd", inputs.window, grain="hour"):
        sums[(t.weekday(), t.hour)] += float(v or 0.0)
    avg = {k: sums[k] / n for k, n in slots.items()}
    grid = [
        {"weekday": WEEKDAYS[wd], "hours": [avg.get((wd, hr)) for hr in range(24)]}
        for wd in range(7)
    ]
    out: dict[str, Any] = {"grid": grid}
    if avg:
        peak = max(avg, key=lambda k: (avg[k], k))
        quiet = min(avg, key=lambda k: (avg[k], k))
        out |= {
            "peak": {"weekday": WEEKDAYS[peak[0]], "hour": peak[1]},
            "peak_value": avg[peak],
            "quiet": {"weekday": WEEKDAYS[quiet[0]], "hour": quiet[1]},
            "quiet_value": avg[quiet],
        }
    return out, out.get("peak_value")


@tool("digest.concentration")
def concentration(ctx: ToolCtx, inputs: PeriodInputs) -> Out:
    """How few markets carry half of the volume."""
    ranked = sorted(
        _by_segment(ctx, "volume_usd", inputs.window, "market").items(),
        key=lambda kv: (-kv[1], kv[0]),
    )
    total = sum(v for _, v in ranked)
    cum, n_half = 0.0, 0
    for _, v in ranked:
        if total and cum >= total / 2:
            break
        cum += v
        n_half += 1
    out: dict[str, Any] = {
        "markets": len(ranked),
        "markets_for_half": n_half if total else 0,
        "half_share": cum / total if total else None,
        "total": total,
        "top": [{"market": m, "value": v} for m, v in ranked[:5]],
    }
    return out, out["half_share"]
