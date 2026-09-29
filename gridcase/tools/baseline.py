"""baseline.expected: expected value and sigma per hour from a seasonal hour-of-week model
with weather terms, fit on the trailing 3 years before a refit date derived from as_of.

Model: value ~ hour-of-week dummies (168; US federal holidays take Sunday's shape) + HDH +
CDH (heating/cooling degree-hours of the BA's load-centre temperature) + a metric-specific
weather regressor (wind speed for the wind proxy, cloud cover for the solar proxy). Least
squares; sigma per hour-of-week bucket is a robust (MAD) residual scale with a floor. The
refit date is the Monday 00:00 UTC <= as_of, so the fit is a pure function of (inputs,
as_of), never of the wall clock.
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
# metric -> (regressor metric, kind). Wind enters as a cubic in speed (capped), cloud linearly.
EXTRA_REGRESSORS: dict[str, tuple[str, str]] = {
    "wind_capacity_factor_proxy": ("wind_load_center", "wind"),
    "solar_capacity_factor_proxy": ("cloud_load_center", "cloud"),
}
EXTRA_WIDTH = {"wind": 3, "cloud": 1, "none": 0}


def refit_date(as_of: datetime) -> datetime:
    day = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def hour_of_week(t: datetime) -> int:
    """Hour-of-week bucket; US federal holidays take Sunday's shape."""
    from gridcase.tools.variance import us_holidays

    weekday = 6 if t.date() in us_holidays(t.year) else t.weekday()
    return weekday * 24 + t.hour


def _extra_cols(kind: str, v: float) -> list[float]:
    if kind == "wind":
        w = min(v, 60.0) / 10.0
        return [w, w * w, w * w * w]
    if kind == "cloud":
        return [v / 100.0]
    return []


class Fit:
    def __init__(
        self,
        coef: np.ndarray,
        sigma: np.ndarray,
        fill: np.ndarray,
        n: int,
        r2: float,
        extra_kind: str = "none",
        extra_fill: float = 0.0,
    ) -> None:
        self.coef, self.sigma, self.fill, self.n, self.r2 = coef, sigma, fill, n, r2
        self.extra_kind, self.extra_fill = extra_kind, extra_fill


def _design(
    ts: list[datetime],
    temps: list[float | None],
    fill: np.ndarray,
    extras: list[float | None],
    extra_kind: str,
    extra_fill: float,
) -> np.ndarray:
    n = len(ts)
    x = np.zeros((n, HOW + 2 + EXTRA_WIDTH[extra_kind]))
    for i, t in enumerate(ts):
        how = hour_of_week(t)
        x[i, how] = 1.0
        tv = temps[i]
        temp: float = float(fill[how]) if tv is None else tv
        x[i, HOW] = max(0.0, BASE_TEMP - temp)
        x[i, HOW + 1] = max(0.0, temp - BASE_TEMP)
        if extra_kind != "none":
            ev = extras[i]
            cols = _extra_cols(extra_kind, extra_fill if ev is None else ev)
            x[i, HOW + 2 : HOW + 2 + len(cols)] = cols
    return x


def _series_map(
    ctx: ToolCtx, metric: str, entity: str, window: tuple[datetime, datetime]
) -> dict[datetime, float]:
    try:
        rows = ctx.metric(metric, entity, window)
    except Exception:  # no data for this entity: the model falls back to fewer terms
        return {}
    return {t: float(v) for t, v in rows if v is not None}


def _temp_map(
    ctx: ToolCtx, entity: str, window: tuple[datetime, datetime]
) -> dict[datetime, float]:
    return _series_map(ctx, "temp_load_center", entity, window)


_FIT_CACHE: dict[tuple[Any, ...], tuple[Fit, datetime]] = {}
_FIT_CACHE_MAX = 512


def fit_baseline(
    ctx: ToolCtx, metric: str, entity: str, dims: dict[str, str] | None = None
) -> tuple[Fit, datetime]:
    """Fit is a pure function of (store contents, metric, entity, dims, as_of). Cached per
    connection and row count so a case does not refit the same model for every tool; the
    receipt still records the training queries and their row hashes."""
    n_rows = ctx.conn.execute("SELECT count(*) FROM observations").fetchone()
    key = (
        id(ctx.conn),
        n_rows[0] if n_rows else 0,
        metric,
        entity,
        tuple(sorted((dims or {}).items())),
        ctx.as_of,
    )
    cached = _FIT_CACHE.get(key)
    if cached is not None:
        _replay_training_queries(ctx, metric, entity, dims)
        return cached
    fit = _fit_uncached(ctx, metric, entity, dims)
    if len(_FIT_CACHE) >= _FIT_CACHE_MAX:
        _FIT_CACHE.clear()
    _FIT_CACHE[key] = fit
    return fit


def _training_window(as_of: datetime) -> tuple[datetime, datetime]:
    refit = refit_date(as_of)
    return refit - timedelta(days=365 * TRAIN_YEARS), refit


def _replay_training_queries(
    ctx: ToolCtx, metric: str, entity: str, dims: dict[str, str] | None
) -> None:
    """Keep the receipt honest on a cache hit: the training data is still 'read'."""
    train_w = _training_window(ctx.as_of)
    ctx.metric(metric, entity, train_w, dims=dims or {})
    _temp_map(ctx, entity, train_w)
    extra = EXTRA_REGRESSORS.get(metric)
    if extra:
        _series_map(ctx, extra[0], entity, train_w)


def _fit_uncached(
    ctx: ToolCtx, metric: str, entity: str, dims: dict[str, str] | None = None
) -> tuple[Fit, datetime]:
    train_w = _training_window(ctx.as_of)
    refit = train_w[1]
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
            fill[hour_of_week(t)] += tv
            counts[hour_of_week(t)] += 1
    fill = np.where(counts > 0, fill / np.maximum(counts, 1), BASE_TEMP)

    extra_kind, extra_fill = "none", 0.0
    elist: list[float | None] = [None] * len(ts)
    extra = EXTRA_REGRESSORS.get(metric)
    if extra:
        emap = _series_map(ctx, extra[0], entity, train_w)
        if len(emap) >= MIN_TRAIN_HOURS:
            extra_kind = extra[1]
            extra_fill = float(np.mean(list(emap.values())))
            elist = [emap.get(t) for t in ts]

    x = _design(ts, tlist, fill, elist, extra_kind, extra_fill)
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ coef
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else 1.0
    floor = max(0.005 * float(np.mean(np.abs(y))), 1e-6)
    sigma = np.full(HOW, floor)
    hows = np.array([hour_of_week(t) for t in ts])
    for h in range(HOW):
        r = resid[hows == h]
        if len(r) >= 4:
            mad = float(np.median(np.abs(r - np.median(r))))
            sigma[h] = max(1.4826 * mad, floor)
    return Fit(coef, sigma, fill, len(ts), r2, extra_kind, extra_fill), refit


def predict(
    ctx: ToolCtx, fit: Fit, entity: str, window: tuple[datetime, datetime], metric: str = ""
) -> list[dict[str, Any]]:
    n = expected_hours(window)
    ts = [window[0] + timedelta(hours=i) for i in range(n)]
    temps = _temp_map(ctx, entity, window)
    elist: list[float | None] = [None] * n
    if fit.extra_kind != "none":
        extra = EXTRA_REGRESSORS.get(metric)
        emap = _series_map(ctx, extra[0], entity, window) if extra else {}
        elist = [emap.get(t) for t in ts]
    x = _design(ts, [temps.get(t) for t in ts], fit.fill, elist, fit.extra_kind, fit.extra_fill)
    yhat = x @ fit.coef
    return [
        {"t": t, "expected": float(yhat[i]), "sigma": float(fit.sigma[hour_of_week(t)])}
        for i, t in enumerate(ts)
    ]


@tool("baseline.expected")
def baseline_expected(
    ctx: ToolCtx, inputs: MetricEntityWindow
) -> tuple[dict[str, Any], float | None]:
    fit, refit = fit_baseline(ctx, inputs.metric, inputs.entity)
    hours = predict(ctx, fit, inputs.entity, inputs.window, inputs.metric)
    return {
        "metric": inputs.metric,
        "entity": inputs.entity,
        "refit_date": refit,
        "n_train": fit.n,
        "r2": fit.r2,
        "hdh_slope": float(fit.coef[HOW]),
        "cdh_slope": float(fit.coef[HOW + 1]),
        "extra_regressor": fit.extra_kind,
        "hours": hours,
    }, fit.r2
