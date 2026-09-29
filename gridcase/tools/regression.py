"""regress.temperature: how much of demand, and of the forecast error, temperature explains.

Three numbers: (1) slope and R^2 of demand on degree-hours within the window; (2) share of
the forecast error explained by the temperature *surprise* (temperature minus the same-hour
mean of the prior 14 days, a proxy for the forecast-weather error we do not store); (3)
implied_minus_actual_mw: the largest gap between temperature-implied demand (the multi-year
baseline evaluated at the window's temperatures) and actual demand, positive when served
load fell short of what the weather implied.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np

from gridcase.tools.base import ToolCtx, ToolError, tool
from gridcase.tools.baseline import fit_baseline, predict
from gridcase.tools.registry import EntityWindow

BASE_TEMP = 18.3


def _series(rows: list[tuple[Any, ...]]) -> dict[datetime, float]:
    return {t: float(v) for t, v in rows if v is not None}


def _degree_features(temps: np.ndarray) -> np.ndarray:
    return np.column_stack(
        [
            np.ones_like(temps),
            np.maximum(0.0, BASE_TEMP - temps),
            np.maximum(0.0, temps - BASE_TEMP),
        ]
    )


def _fit(temps: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float]:
    x = _degree_features(temps)
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ coef
    ss = float(((y - y.mean()) ** 2).sum())
    return coef, (1.0 - float((resid**2).sum()) / ss) if ss > 0 else 1.0


def _thermal(temp: float) -> float:
    """Degree-hours away from the comfort band, either direction."""
    return max(0.0, BASE_TEMP - temp) + max(0.0, temp - BASE_TEMP)


def _r2_1d(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1] ** 2)


@tool("regress.temperature")
def regress_temperature(ctx: ToolCtx, inputs: EntityWindow) -> tuple[dict[str, Any], float | None]:
    start, end = inputs.window
    demand = _series(ctx.metric("demand", inputs.entity, inputs.window))
    temp = _series(ctx.metric("temp_load_center", inputs.entity, inputs.window))
    ts = sorted(set(demand) & set(temp))
    if len(ts) < 24:
        raise ToolError(f"too few demand/temperature hours for {inputs.entity}: {len(ts)}")
    y = np.array([demand[t] for t in ts])
    x = np.array([temp[t] for t in ts])
    coef, r2 = _fit(x, y)
    cooling = float(np.mean(x)) >= BASE_TEMP
    slope = float(coef[2]) if cooling else float(coef[1])

    # Event hours: the anomalous run the case was opened on, else the whole window.
    ev_start = inputs.event_start if inputs.event_start is not None else start
    ev_end = inputs.event_end if inputs.event_end is not None else end
    ev_ts = [t for t in ts if ev_start <= t < ev_end]
    short_event = len(ev_ts) < 6  # too short a run to regress on: event stats stay unknown
    if not ev_ts:
        ev_ts = ts

    # Thermal surprise vs the prior 14 days, same hour of day: degree-hours beyond what the
    # recent climatology implied. A weather-driven miss has the right sign (more extreme than
    # expected -> more demand than forecast); load shed has the wrong one (colder -> less
    # served load). share_explained is R^2 of the error on that surprise over the event
    # hours; sign_consistent is 1 when the correlation is positive, 0 when negative, absent
    # when undefined (fewer than 6 event hours or no variation).
    prior = _series(
        ctx.metric("temp_load_center", inputs.entity, (start - timedelta(days=14), start))
    )
    err = _series(ctx.metric("forecast_error_mw", inputs.entity, inputs.window))
    share: float | None = None
    sign_consistent: int | None = None
    share_window: float | None = None
    sign_window: int | None = None
    if prior:
        by_hour: dict[int, list[float]] = {}
        for t, v in prior.items():
            by_hour.setdefault(t.hour, []).append(v)
        clim = {h: sum(v) / len(v) for h, v in by_hour.items()}

        def _surprise_stats(hours: list[datetime]) -> tuple[float | None, int | None]:
            common = [t for t in hours if t in err and t.hour in clim]
            if len(common) < 6:
                return None, None
            surprise = np.array([_thermal(temp[t]) - _thermal(clim[t.hour]) for t in common])
            e = np.array([err[t] for t in common])
            r2 = _r2_1d(surprise, e)
            if r2 is None:
                return None, None
            return r2, (1 if float(np.corrcoef(surprise, e)[0, 1]) > 0 else 0)

        share_window, sign_window = _surprise_stats(ts)
        if not short_event:
            share, sign_consistent = _surprise_stats(ev_ts)

    # Temperature-implied demand: the multi-year baseline (hour-of-week + degree-hours) at the
    # window's actual temperatures. A short recent fit cannot extrapolate an extreme cold or
    # heat event; three winters can.
    sigma = np.full(len(ts), float(np.std(y)) or 1.0)
    try:
        fit, _ = fit_baseline(ctx, "demand", inputs.entity)
        pred = {h["t"]: h for h in predict(ctx, fit, inputs.entity, inputs.window)}
        implied = np.array([pred[t]["expected"] for t in ts])
        sigma = np.array([pred[t]["sigma"] for t in ts])
        implied_basis = "baseline"
    except ToolError:
        implied = _degree_features(x) @ coef  # fall back to the in-window fit
        implied_basis = "in_window"
    gap = implied - y
    # Significance of the served-load shortfall over the event hours: per-hour z against the
    # baseline's own residual scale; the mean over the run is the robust statistic.
    ev = np.array([t in set(ev_ts) for t in ts])
    gap_z = gap[ev] / np.maximum(sigma[ev], 1e-9)
    max_err = max((abs(v) for v in err.values()), default=0.0)
    return {
        "entity": inputs.entity,
        "n": len(ts),
        "slope_mw_per_degc": slope,
        "r2": r2,
        "share_explained": share,
        "sign_consistent": sign_consistent,
        "share_explained_window": share_window,
        "sign_consistent_window": sign_window,
        "short_event": short_event,
        "error_explained_mw": (share * max_err) if share is not None else None,
        "implied_minus_actual_mw": float(gap[ev].max()),
        "implied_minus_actual_mean_mw": float(gap[ev].mean()),
        "implied_minus_actual_z": float(gap_z.max()),
        # unknown on a short run: a few hours cannot establish a sustained shortfall
        "implied_minus_actual_mean_z": None if short_event else float(gap_z.mean()),
        "event_hours": int(ev.sum()),
        "implied_basis": implied_basis,
        "regime": "cooling" if cooling else "heating",
    }, r2
