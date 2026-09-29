"""baseline.expected: expected value and sigma per hour from a seasonal hour-of-week model
with temperature terms, fit on the trailing 3 years before a refit date derived from as_of.

Model: value ~ hour_of_week dummies (168) + HDH + CDH (heating/cooling degree-hours of the
BA's load-centre temperature). Fit by least squares; sigma per hour-of-week bucket is a
robust (MAD) residual scale with a floor. The refit date is the Monday 00:00 UTC <= as_of, so
the fit is a pure function of (inputs, as_of), never of the wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np

from gridcase.tools.base import ToolCtx, ToolError, expected_hours, tool
from gridcase.tools.registry import MetricEntityWindow

TRAIN_YEARS = 3
MIN_TRAIN_HOURS = 24 * 14
BASE_TEMP = 18.3
HOW = 168


def refit_date(as_of: datetime) -> datetime:
    day = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def _features(ts: list[datetime], temps: list[float | None], fill: np.ndarray) -> np.ndarray:
    n = len(ts)
    x = np.zeros((n, HOW + 2))
    for i, t in enumerate(ts):
        how = t.weekday() * 24 + t.hour
        x[i, how] = 1.0
        tv = temps[i]
        temp: float = float(fill[how]) if tv is None else tv
        x[i, HOW] = max(0.0, BASE_TEMP - temp)
        x[i, HOW + 1] = max(0.0, temp - BASE_TEMP)
    return x


def _temp_map(
    ctx: ToolCtx, entity: str, window: tuple[datetime, datetime]
) -> dict[datetime, float]:
    try:
        rows = ctx.metric("temp_load_center", entity, window)
    except Exception:  # no weather for this entity: model falls back to hour-of-week only
        return {}
    return {t: float(v) for t, v in rows if v is not None}


class Fit:
    def __init__(
        self, coef: np.ndarray, sigma: np.ndarray, fill: np.ndarray, n: int, r2: float
    ) -> None:
        self.coef, self.sigma, self.fill, self.n, self.r2 = coef, sigma, fill, n, r2


def fit_baseline(
    ctx: ToolCtx, metric: str, entity: str, dims: dict[str, str] | None = None
) -> tuple[Fit, datetime]:
    refit = refit_date(ctx.as_of)
    train_w = (refit - timedelta(days=365 * TRAIN_YEARS), refit)
    rows = [(r[0], r[-1]) for r in ctx.metric(metric, entity, train_w, dims=dims or {})]
    ts = [t for t, v in rows if v is not None]
    y = np.array([float(v) for _, v in rows if v is not None])
    if len(ts) < MIN_TRAIN_HOURS:
        raise ToolError(
            f"insufficient history for {metric}/{entity}: {len(ts)} hours before {refit:%Y-%m-%d}"
        )
    temps = _temp_map(ctx, entity, train_w)
    tlist = [temps.get(t) for t in ts]
    fill = np.zeros(HOW)
    counts = np.zeros(HOW)
    for t, tv in zip(ts, tlist, strict=True):
        if tv is not None:
            fill[t.weekday() * 24 + t.hour] += tv
            counts[t.weekday() * 24 + t.hour] += 1
    fill = np.where(counts > 0, fill / np.maximum(counts, 1), BASE_TEMP)
    x = _features(ts, tlist, fill)
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ coef
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else 1.0
    floor = max(0.005 * float(np.mean(np.abs(y))), 1.0)
    sigma = np.full(HOW, floor)
    hows = np.array([t.weekday() * 24 + t.hour for t in ts])
    for h in range(HOW):
        r = resid[hows == h]
        if len(r) >= 4:
            mad = float(np.median(np.abs(r - np.median(r))))
            sigma[h] = max(1.4826 * mad, floor)
    return Fit(coef, sigma, fill, len(ts), r2), refit


def predict(
    ctx: ToolCtx, fit: Fit, entity: str, window: tuple[datetime, datetime]
) -> list[dict[str, Any]]:
    n = expected_hours(window)
    ts = [window[0] + timedelta(hours=i) for i in range(n)]
    temps = _temp_map(ctx, entity, window)
    x = _features(ts, [temps.get(t) for t in ts], fit.fill)
    yhat = x @ fit.coef
    return [
        {"t": t, "expected": float(yhat[i]), "sigma": float(fit.sigma[t.weekday() * 24 + t.hour])}
        for i, t in enumerate(ts)
    ]


@tool("baseline.expected")
def baseline_expected(
    ctx: ToolCtx, inputs: MetricEntityWindow
) -> tuple[dict[str, Any], float | None]:
    fit, refit = fit_baseline(ctx, inputs.metric, inputs.entity)
    hours = predict(ctx, fit, inputs.entity, inputs.window)
    return {
        "metric": inputs.metric,
        "entity": inputs.entity,
        "refit_date": refit,
        "n_train": fit.n,
        "r2": fit.r2,
        "hdh_slope": float(fit.coef[HOW]),
        "cdh_slope": float(fit.coef[HOW + 1]),
        "hours": hours,
    }, fit.r2
