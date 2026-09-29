"""regress.temperature: how much of demand, and of the forecast error, temperature explains.

Four numbers: (1) slope and R^2 of demand on degree-hours within the window; (2) share of
the forecast error explained by the temperature *surprise* against the same-hour mean of the
prior 14 days (share_explained*, kept for comparability); (3) forecast_weather_*: "did the
weather turn out different from what the forecaster assumed?" The day-ahead forecast is
inverted through the fitted demand baseline into the degree-hours it priced in; the weather-
implied error per hour is the baseline's degree-hour slope times (actual minus priced-in
degree-hours). forecast_weather_share is the fraction of the error's energy that weather-
implied error accounts for, 1 - sum((err - w)^2) / sum(err^2), floored at 0 (a level
measure: an unforecast heatwave is a near-constant offset a correlation cannot see);
forecast_weather_sign is 1 when they point the same way. Hours outside the fit's training
degree-hour range are not inverted (a mild-history model cannot say what Uri's cold priced
in); with too few usable hours or a poor fit the climatology share stands in and
forecast_weather_basis says so. (4) implied_minus_actual_mw: the largest gap between
temperature-implied demand (the multi-year baseline at the window's temperatures) and actual
demand, positive when served load fell short of what the weather implied.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np

from gridcase.tools.base import ToolCtx, ToolError, tool
from gridcase.tools.baseline import HOW, Fit, fit_baseline, hour_of_week, predict
from gridcase.tools.registry import EntityWindow

BASE_TEMP = 18.3
# The forecast is inverted through the demand baseline only when the fit means something and
# its degree-hour coefficient on the actual temperature's side is positive.
MIN_FIT_R2 = 0.2
MIN_MW_PER_DEGREE_HOUR = 1e-6
SUPPORT_MARGIN = 1.2  # invert only within 1.2x the training degree-hour range
MIN_SURPRISE_HOURS = 6
MIN_SHARE_FOR_SIGN = 0.05  # below this the forecast-relative sign is reported unknown
MIN_FORECAST_COVERAGE = 0.8  # share of window hours that must be invertible
REFERENCE_DAYS = 14  # the usual model-vs-forecast offset is measured over these prior days
MIN_REFERENCE_HOURS = 72  # at least three days of reference hours


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


def _surprise_stats(
    hours: list[datetime], surprise: dict[datetime, float], err: dict[datetime, float]
) -> tuple[float | None, int | None]:
    """R^2 of the forecast error on a thermal surprise, and whether the sign is the weather
    one (more extreme than expected -> more demand than forecast). Unknown with fewer than
    MIN_SURPRISE_HOURS common hours or no variation."""
    common = [t for t in hours if t in err and t in surprise]
    if len(common) < MIN_SURPRISE_HOURS:
        return None, None
    x = np.array([surprise[t] for t in common])
    e = np.array([err[t] for t in common])
    r2 = _r2_1d(x, e)
    if r2 is None:
        return None, None
    return r2, (1 if float(np.corrcoef(x, e)[0, 1]) > 0 else 0)


def _weather_implied_error(fit: Fit, t: datetime, temp: float, forecast: float) -> float | None:
    """MW of forecast error the weather surprise implies for hour t: the baseline's degree-hour
    slope (on the actual temperature's side of 18.3 C) times the actual degree-hours minus
    those that make the baseline equal the forecast (the weather the forecaster priced in).
    None when the slope is not positive or either side lies outside the fit's support."""
    heating = temp < BASE_TEMP
    k = float(fit.coef[HOW] if heating else fit.coef[HOW + 1])
    support = SUPPORT_MARGIN * (fit.hdh_max if heating else fit.cdh_max)
    if not np.isfinite(k) or k <= MIN_MW_PER_DEGREE_HOUR or support <= 0:
        return None
    actual_dh = _thermal(temp)
    priced_dh = max(0.0, (forecast - float(fit.coef[hour_of_week(t)])) / k)
    if actual_dh > support or priced_dh > support:
        return None
    return k * (actual_dh - priced_dh)


def _hourly_median(values: dict[datetime, float]) -> dict[int, float] | None:
    """Median per hour of day (key -1: overall median, for hours with too few samples).
    None when the reference period is too thin to say what 'usual' is."""
    if len(values) < MIN_REFERENCE_HOURS:
        return None
    by_hour: dict[int, list[float]] = {}
    for t, v in values.items():
        by_hour.setdefault(t.hour, []).append(v)
    out = {h: float(np.median(v)) for h, v in by_hour.items() if len(v) >= 3}
    out[-1] = float(np.median(list(values.values())))
    return out


def _explained_stats(
    hours: list[datetime], implied: dict[datetime, float], err: dict[datetime, float]
) -> tuple[float | None, int | None]:
    """Fraction of the error's energy the weather-implied error accounts for, floored at 0,
    and whether the two point the same way. Unknown with too few hours or no error."""
    common = [t for t in hours if t in err and t in implied]
    if len(common) < MIN_SURPRISE_HOURS:
        return None, None
    e = np.array([err[t] for t in common])
    w = np.array([implied[t] for t in common])
    energy = float((e**2).sum())
    if energy == 0:
        return None, None
    share = max(0.0, 1.0 - float(((e - w) ** 2).sum()) / energy)
    # When the weather surprise accounts for next to nothing, its direction is noise: unknown.
    if share < MIN_SHARE_FOR_SIGN:
        return share, None
    return share, (1 if float((e * w).sum()) > 0 else 0)


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
        clim_surprise = {
            t: _thermal(temp[t]) - _thermal(clim[t.hour]) for t in ts if t.hour in clim
        }
        share_window, sign_window = _surprise_stats(ts, clim_surprise, err)
        if not short_event:
            share, sign_consistent = _surprise_stats(ev_ts, clim_surprise, err)

    # Temperature-implied demand: the multi-year baseline (hour-of-week + degree-hours) at the
    # window's actual temperatures. A short recent fit cannot extrapolate an extreme cold or
    # heat event; three winters can.
    sigma = np.full(len(ts), float(np.std(y)) or 1.0)
    fit: Fit | None = None
    try:
        fit, _ = fit_baseline(ctx, "demand", inputs.entity)
        pred = {h["t"]: h for h in predict(ctx, fit, inputs.entity, inputs.window)}
        implied = np.array([pred[t]["expected"] for t in ts])
        sigma = np.array([pred[t]["sigma"] for t in ts])
        implied_basis = "baseline"
    except ToolError:
        implied = _degree_features(x) @ coef  # fall back to the in-window fit
        implied_basis = "in_window"

    # Forecast-relative weather: the error the weather surprise implies, relative to what the
    # day-ahead forecast priced in. A heatwave the forecast priced in gives weather no credit;
    # one it missed does. Without a usable fit the climatology share stands in, and
    # forecast_weather_basis says so.
    fw_share, fw_sign = share, sign_consistent
    fw_share_window, fw_sign_window = share_window, sign_window
    fw_basis = "climatology"
    fw_hours = 0
    if fit is not None and fit.r2 >= MIN_FIT_R2:
        ref = (start - timedelta(days=REFERENCE_DAYS), start)
        forecast = _series(ctx.metric("demand_forecast", inputs.entity, (ref[0], end)))
        ref_temp = {**prior, **temp}
        raw: dict[datetime, float] = {}
        for t, fc in forecast.items():
            if t in ref_temp:
                w = _weather_implied_error(fit, t, ref_temp[t], fc)
                if w is not None:
                    raw[t] = w
        # Our baseline and the operator's model differ systematically (season, trend); that
        # offset is not weather. Measure the surprise against its usual size over the prior
        # REFERENCE_DAYS, same hour of day (the climatology statistic's reference period).
        offset = _hourly_median({t: v for t, v in raw.items() if ref[0] <= t < ref[1]})
        weather_err: dict[datetime, float] = {}
        if offset is not None:
            for t in ts:
                if t in raw:
                    weather_err[t] = raw[t] - offset.get(t.hour, offset[-1])
        fw_hours = len(weather_err)
        ev_usable = sum(1 for t in ev_ts if t in weather_err)
        # Invert only when it covers the window and the event: dropping the event's
        # out-of-range hours would let the ordinary hours decide about the extraordinary ones.
        if fw_hours >= max(
            MIN_SURPRISE_HOURS, MIN_FORECAST_COVERAGE * len(ts)
        ) and ev_usable >= min(MIN_SURPRISE_HOURS, len(ev_ts)):
            fw_basis = "forecast"
            fw_share_window, fw_sign_window = _explained_stats(ts, weather_err, err)
            fw_share, fw_sign = (
                (None, None) if short_event else _explained_stats(ev_ts, weather_err, err)
            )
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
        "forecast_weather_share": fw_share,
        "forecast_weather_sign": fw_sign,
        "forecast_weather_share_window": fw_share_window,
        "forecast_weather_sign_window": fw_sign_window,
        "forecast_weather_basis": fw_basis,
        "forecast_weather_hours": fw_hours,
        "short_event": short_event,
        "error_explained_mw": (share * max_err) if share is not None else None,
        "forecast_weather_error_mw": (fw_share * max_err) if fw_share is not None else None,
        "implied_minus_actual_mw": float(gap[ev].max()),
        "implied_minus_actual_mean_mw": float(gap[ev].mean()),
        "implied_minus_actual_z": float(gap_z.max()),
        # unknown on a short run: a few hours cannot establish a sustained shortfall
        "implied_minus_actual_mean_z": None if short_event else float(gap_z.mean()),
        "event_hours": int(ev.sum()),
        "implied_basis": implied_basis,
        "regime": "cooling" if cooling else "heating",
    }, r2
