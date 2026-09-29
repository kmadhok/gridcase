"""The KPI alarm: plain rolling statistics on one governed KPI (ontology/alarm.yaml).

An hour is abnormal when the KPI sits more than k standard deviations from its own average
over the `lookback_days` before the recent window; an alarm is `min_run` abnormal hours in a
row on the same side. The result is a Receipt (tool "kpi.alarm") with the compiled SQL, so a
case opened by the alarm can show exactly why. Pure function of (entity, as_of, params).
"""

from __future__ import annotations

import math
import statistics
import time
from datetime import date, datetime, timedelta
from functools import cache
from typing import Any

import yaml
from pydantic import BaseModel, Field

from gridcase.config import ONTOLOGY_DIR
from gridcase.ledger.schema import Receipt
from gridcase.tools.base import ToolCtx

HOUR = timedelta(hours=1)


class AlarmParams(BaseModel):
    kpi: str = "forecast_error_pct"
    lookback_days: int = Field(default=28, ge=1)
    k_sigma: float = Field(default=3.0, gt=0)
    min_run: int = Field(default=3, ge=1)
    recent_hours: int = Field(default=24, ge=1)
    settle_hours: int = Field(default=3, ge=0)
    min_history_days: int = Field(default=7, ge=1)
    # An hour whose day-ahead forecast is below this share of the usual forecast for that hour
    # of day is bad data (real grid events move demand, not yesterday's forecast).
    bad_forecast_ratio: float = Field(default=0.6, gt=0, lt=1)
    # Skip the US daylight-saving switch days (EIA's national sum misaligns on them).
    skip_dst_days: bool = False


class _Config(BaseModel):
    defaults: AlarmParams
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


@cache
def _config() -> _Config:
    return _Config.model_validate(yaml.safe_load((ONTOLOGY_DIR / "alarm.yaml").read_text()))


def alarm_params(entity: str) -> AlarmParams:
    cfg = _config()
    over = cfg.overrides.get(entity)
    return (
        cfg.defaults if not over else AlarmParams.model_validate(cfg.defaults.model_dump() | over)
    )


def dst_switch_days(year: int) -> set[date]:
    """US clock changes: second Sunday of March, first Sunday of November, plus the Monday
    after each (UTC dates, which cover the local Sunday evening)."""

    def nth_sunday(month: int, n: int) -> date:
        d = date(year, month, 1)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    days = {nth_sunday(3, 2), nth_sunday(11, 1)}
    return days | {d + timedelta(days=1) for d in days}


def _r(x: float | None) -> float | None:
    return None if x is None else round(float(x), 6)


def check_kpi(conn: Any, entity: str, as_of: datetime, params: AlarmParams) -> Receipt:
    t0 = time.perf_counter()
    ctx = ToolCtx(conn=conn, as_of=as_of)
    judged_until = as_of - timedelta(hours=params.settle_hours)
    judged_from = judged_until - timedelta(hours=params.recent_hours)
    history_from = judged_from - timedelta(days=params.lookback_days)

    forecast = {
        t: float(v)
        for t, v in ctx.metric("demand_forecast", entity, (history_from, judged_until))
        if v is not None
    }
    by_hour: dict[int, list[float]] = {}
    for t, v in forecast.items():
        if t < judged_from:
            by_hour.setdefault(t.hour, []).append(v)
    usual = {h: statistics.median(vs) for h, vs in by_hour.items()}
    dst: set[date] = set()
    if params.skip_dst_days:
        for y in range(history_from.year, judged_until.year + 1):
            dst |= dst_switch_days(y)

    def verdict(t: datetime, v: float) -> str:
        if t.date() in dst:
            return "dst"
        f = forecast.get(t)
        if f is not None and t.hour in usual and f < params.bad_forecast_ratio * usual[t.hour]:
            return "bad_forecast"
        if v == 0.0:  # exactly 0: EIA placeholder (demand = forecast)
            return "placeholder"
        return "ok"

    history = [
        float(v)
        for t, v in ctx.metric(params.kpi, entity, (history_from, judged_from))
        if v is not None and verdict(t, float(v)) == "ok"
    ]
    recent_rows = [
        (t, float(v), verdict(t, float(v)))
        for t, v in ctx.metric(params.kpi, entity, (judged_from, judged_until))
        if v is not None
    ]
    placeholders = sum(1 for *_, k in recent_rows if k == "placeholder")
    bad_forecast = sum(1 for *_, k in recent_rows if k == "bad_forecast")
    dst_skipped = sum(1 for *_, k in recent_rows if k == "dst")
    recent = [(t, v) for t, v, k in recent_rows if k == "ok"]

    n = len(history)
    mean = sum(history) / n if n else 0.0
    std = math.sqrt(sum((v - mean) ** 2 for v in history) / n) if n else 0.0
    insufficient = n < params.min_history_days * 24 or std <= 0.0

    runs: list[list[tuple[datetime, float, float]]] = []
    run: list[tuple[datetime, float, float]] = []
    for t, v in recent:
        z = (v - mean) / std if not insufficient else 0.0
        extreme = abs(z) > params.k_sigma
        joins = run and extreme and t - run[-1][0] == HOUR and (z > 0) == (run[-1][2] > 0)
        if joins:
            run.append((t, v, z))
            continue
        if len(run) >= params.min_run:
            runs.append(run)
        run = [(t, v, z)] if extreme else []
    if len(run) >= params.min_run:
        runs.append(run)

    alarm = bool(runs) and not insufficient
    worst_src = runs[-1] if alarm else [(t, v, (v - mean) / std if std else 0.0) for t, v in recent]
    worst = max(worst_src, key=lambda x: abs(x[2]), default=None)
    output: dict[str, Any] = {
        "kpi": params.kpi,
        "entity": entity,
        "alarm": alarm,
        "direction": ("above" if runs[-1][0][2] > 0 else "below") if alarm else None,
        "run_start": runs[-1][0][0].isoformat() if alarm else None,
        "run_end": runs[-1][-1][0].isoformat() if alarm else None,
        "run_hours": len(runs[-1]) if alarm else 0,
        "worst_hour": worst[0].isoformat() if worst else None,
        "worst_value": _r(worst[1]) if worst else None,
        "worst_z": _r(worst[2]) if worst else None,
        "mean": _r(mean),
        "std": _r(std),
        "threshold_low": _r(mean - params.k_sigma * std),
        "threshold_high": _r(mean + params.k_sigma * std),
        "k_sigma": params.k_sigma,
        "lookback_days": params.lookback_days,
        "min_run": params.min_run,
        "judged_from": judged_from.isoformat(),
        "judged_until": judged_until.isoformat(),
        "n_recent": len(recent),
        "n_history": n,
        "n_placeholder": placeholders,
        "n_bad_forecast": bad_forecast,
        "n_dst_skipped": dst_skipped,
        "insufficient": insufficient,
    }
    return Receipt.build(
        tool="kpi.alarm",
        inputs={"entity": entity, **params.model_dump()},
        as_of=as_of,
        output=output,
        data_version=ctx.data_version(),
        effect_size=output["worst_z"],
        runtime_ms=int((time.perf_counter() - t0) * 1000),
        sql=ctx.sql_log,
    )
