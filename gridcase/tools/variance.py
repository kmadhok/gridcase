"""variance.plan_vs_actual: demand minus forecast by hour, with calendar context."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from gridcase.tools.base import ToolCtx, ToolError, tool
from gridcase.tools.registry import EntityWindow


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def us_holidays(year: int) -> dict[date, str]:
    return {
        date(year, 1, 1): "new_year",
        _nth_weekday(year, 1, 0, 3): "mlk",
        _nth_weekday(year, 2, 0, 3): "presidents",
        _last_weekday(year, 5, 0): "memorial",
        date(year, 6, 19): "juneteenth",
        date(year, 7, 4): "independence",
        _nth_weekday(year, 9, 0, 1): "labor",
        _nth_weekday(year, 10, 0, 2): "columbus",
        date(year, 11, 11): "veterans",
        _nth_weekday(year, 11, 3, 4): "thanksgiving",
        date(year, 12, 25): "christmas",
    }


def dst_boundaries(year: int) -> dict[date, str]:
    return {
        _nth_weekday(year, 3, 6, 2): "spring_forward",
        _nth_weekday(year, 11, 6, 1): "fall_back",
    }


def calendar_events(start: datetime, end: datetime) -> list[str]:
    """Holidays and DST changes with a date inside [start, end)."""
    out: list[str] = []
    d = start.date()
    while datetime.combine(d, datetime.min.time()) < end:
        for name in [us_holidays(d.year).get(d)]:
            if name:
                out.append(f"holiday:{name}")
        for name in [dst_boundaries(d.year).get(d)]:
            if name:
                out.append(f"dst:{name}")
        d += timedelta(days=1)
    return out


@tool("variance.plan_vs_actual")
def plan_vs_actual(ctx: ToolCtx, inputs: EntityWindow) -> tuple[dict[str, Any], float | None]:
    mw = {
        t: float(v)
        for t, v in ctx.metric("forecast_error_mw", inputs.entity, inputs.window)
        if v is not None
    }
    pct = {
        t: float(v)
        for t, v in ctx.metric("forecast_error_pct", inputs.entity, inputs.window)
        if v is not None
    }
    if not mw:
        raise ToolError(f"no demand/forecast pairs for {inputs.entity} in window")
    worst_t = min(mw, key=lambda t: mw[t])
    events = calendar_events(*inputs.window)
    hours = [{"t": t, "error_mw": mw[t], "error_pct": pct.get(t)} for t in sorted(mw)]
    return {
        "entity": inputs.entity,
        "n": len(mw),
        "mean_error_mw": sum(mw.values()) / len(mw),
        "min_error_mw": min(mw.values()),
        "max_error_mw": max(mw.values()),
        "min_error_pct": min(pct.values()) if pct else None,
        "max_error_pct": max(pct.values()) if pct else None,
        "worst_t": worst_t,
        "calendar_events": events,
        "calendar_event_in_window": 1 if events else 0,
        "hours": hours,
    }, min(pct.values()) if pct else None
