"""Detector calibration Part B: judge weather against the operator's forecast.

Requirements (docs/plans/2026-09-29_detector_calibration.md, Part B):
- regress.temperature reports forecast_weather_share / forecast_weather_sign (event hours) and
  *_window twins: how much of the forecast error the weather-implied error accounts for, where
  the weather-implied error comes from the forecast-relative thermal surprise = degree-hours
  of the actual temperature minus those the day-ahead forecast implied under the fitted
  demand baseline. "Did the weather turn out different from what the forecaster assumed?",
  not "is demand high for the temperature vs a 14-day climatology?".
- A hot day the forecast already priced in gives weather no credit, even when something else
  moves the error in step with the heat; an unforecast heatwave gets the credit.
- The same hot day with a 12% supply loss reads as a supply shortfall, not weather.
- With a poor demand fit, or weather outside the fit's training range, it falls back to the
  climatology surprise and says so.
- H1/H2/H4 judge weather on the forecast-relative fields.
- Leak-free: rows after as_of change nothing.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pytest

from gridcase.planner.falsifier import evaluate, parse
from gridcase.planner.hypotheses import load_templates
from gridcase.store.db import append_observations, connect
from gridcase.store.models import Observation
from gridcase.tools.base import run_tool

T0 = datetime(2023, 4, 3)  # a Monday
WEEKS = 12
AS_OF = T0 + timedelta(weeks=WEEKS - 1, days=3)  # Thursday; refit Monday = T0 + 11 weeks
EVENT = (AS_OF - timedelta(days=2), AS_OF)  # the last two days before as_of are hot
WINDOW = (AS_OF - timedelta(days=3), AS_OF)
ZONES = ("wz/austin", "wz/dallas", "wz/houston", "wz/san_antonio")
K_COOL = 1_500.0  # MW per degree-hour above 18.3 C


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "w.duckdb")
    yield conn
    conn.close()


def _obs(series: str, entity: str, t: datetime, v: float, lag_h: int = 1) -> Observation:
    seen = t + timedelta(hours=lag_h)
    return Observation(
        series_id=series,
        entity_id=entity,
        valid_time=t,
        value=v,
        observed_at=seen,
        available_at=seen,
        source="test",
    )


def _base(t: datetime) -> float:
    return 30_000 + 4_000 * math.sin((t.hour - 6) / 24 * 2 * math.pi)


def _cdh(temp: float) -> float:
    return max(0.0, temp - 18.3)


def load_store(
    db: duckdb.DuckDBPyConnection,
    *,
    heat_c: float = 6.0,
    priced: bool = True,
    extra_mw: float = 0.0,
    loss_share: float = 0.0,
    temp_relevant: bool = True,
    forecast_bias_mw: float = 0.0,
    seed: int = 11,
) -> None:
    """Demand = hour-of-day base + K_COOL x CDH(temp) + noise. The event days are heat_c
    hotter than usual. priced: the day-ahead forecast knew the heat. extra_mw: a non-weather
    surge (say, an unforecast industrial load) through the event hours, so the error steps up
    in step with the heat. loss_share: a supply loss of that share of demand in the event
    hours (served load and generation fall together)."""
    rng = np.random.default_rng(seed)
    day_offset: dict[int, float] = {}
    rows: list[Observation] = []
    for i in range(24 * 7 * WEEKS):
        t = T0 + timedelta(hours=i)
        day = (t - T0).days
        if day not in day_offset:
            day_offset[day] = float(rng.uniform(-3, 3))
        diurnal = 5 * math.sin((t.hour - 9) / 24 * 2 * math.pi)
        in_event = EVENT[0] <= t < EVENT[1]
        temp = 25 + diurnal + day_offset[day] + (heat_c if in_event else 0.0)
        assumed = temp if (priced or not in_event) else temp - heat_c
        weather = K_COOL * _cdh(temp) if temp_relevant else 0.0
        weather_fc = K_COOL * _cdh(assumed) if temp_relevant else 0.0
        noise = float(rng.normal(0, 300)) if temp_relevant else float(rng.normal(0, 8_000))
        demand = _base(t) + weather + noise
        forecast = _base(t) + weather_fc + forecast_bias_mw + float(rng.normal(0, 300))
        if in_event:
            demand += extra_mw
            demand -= loss_share * demand
        rows += [
            _obs("eia.D", "ERCO", t, demand),
            _obs("eia.DF", "ERCO", t, forecast, lag_h=-14),
            _obs("eia.NG", "ERCO", t, demand),
            _obs("eia.TI", "ERCO", t, 0.0),
        ]
        rows += [_obs("om.temp_2m", z, t, temp) for z in ZONES]
    append_observations(db, rows, dedupe_unchanged=False)


def _regress(db: duckdb.DuckDBPyConnection, as_of: datetime = AS_OF) -> dict[str, Any]:
    return run_tool(
        db,
        "regress.temperature",
        {"entity": "ERCO", "window": WINDOW, "event_start": EVENT[0], "event_end": EVENT[1]},
        as_of,
    ).output


def test_forecast_weather_share_is_near_zero_when_the_forecast_priced_the_heat(
    db: duckdb.DuckDBPyConnection,
) -> None:
    # A daytime non-weather surge moves the error in step with the heat. Against a 14-day
    # climatology the hot days look like a weather miss; against what the forecast assumed
    # (the heat was in the forecast) they do not.
    load_store(db, heat_c=6.0, priced=True, extra_mw=4_000.0)
    out = _regress(db)
    assert out["forecast_weather_basis"] == "forecast"
    assert out["share_explained_window"] is not None and out["share_explained_window"] > 0.5
    assert out["sign_consistent_window"] == 1
    assert out["forecast_weather_share_window"] is not None
    assert out["forecast_weather_share_window"] < 0.2
    assert out["forecast_weather_share"] is not None and out["forecast_weather_share"] < 0.2


def test_an_unforecast_heatwave_is_credited_to_the_weather(db: duckdb.DuckDBPyConnection) -> None:
    load_store(db, heat_c=6.0, priced=False)
    out = _regress(db)
    assert out["forecast_weather_basis"] == "forecast"
    assert out["forecast_weather_share"] is not None and out["forecast_weather_share"] > 0.5
    assert out["forecast_weather_sign"] == 1
    assert out["forecast_weather_share_window"] > 0.5
    assert out["forecast_weather_sign_window"] == 1


def _falsified(hid: str, outputs: dict[str, Any]) -> bool | None:
    template = next(t for t in load_templates() if t.id == hid)
    return evaluate(parse(template.falsifier), outputs)


def test_same_day_with_a_12pct_fuel_loss_is_a_supply_shortfall_not_weather(
    db: duckdb.DuckDBPyConnection,
) -> None:
    load_store(db, heat_c=6.0, priced=True, loss_share=0.12)
    reg = _regress(db)
    gap = run_tool(
        db,
        "anomaly.detect",
        {"metric": "supply_gap_mw", "entity": "ERCO", "window": WINDOW, "event_start": EVENT[0]},
        AS_OF,
    ).output
    outputs = {
        "regress.temperature": reg,
        "anomaly.detect": gap,
        "peer.compare": {"max_corr": 0.1},  # neighbours did not see it
    }
    assert reg["forecast_weather_share_window"] is not None
    assert reg["forecast_weather_share_window"] < 0.5
    assert _falsified("H1_WEATHER_DEMAND", outputs) is True
    assert _falsified("H2_SUPPLY_SHORTFALL", outputs) is False


def test_a_standing_offset_between_our_model_and_the_forecast_is_not_weather(
    db: duckdb.DuckDBPyConnection,
) -> None:
    # The operator's model runs 3,000 MW above ours every hour (found on the fixture: June
    # 2023). Inverting the forecast reads that offset as "milder than assumed"; measured
    # against its usual size over the prior two weeks it vanishes, and a 12% supply loss on
    # a priced-in hot day gets no weather credit.
    load_store(db, heat_c=6.0, priced=True, loss_share=0.12, forecast_bias_mw=3_000.0)
    out = _regress(db)
    assert out["forecast_weather_basis"] == "forecast"
    assert out["forecast_weather_share"] is not None and out["forecast_weather_share"] < 0.2
    assert out["forecast_weather_share_window"] < 0.2


def test_forecast_weather_falls_back_to_climatology_when_the_fit_is_poor(
    db: duckdb.DuckDBPyConnection,
) -> None:
    # Demand is noise unrelated to temperature or the hour: the fit explains nothing, so the
    # forecast cannot be inverted into an assumed temperature.
    load_store(db, heat_c=6.0, priced=True, temp_relevant=False)
    out = _regress(db)
    assert out["forecast_weather_basis"] == "climatology"
    assert out["forecast_weather_share_window"] == out["share_explained_window"]
    assert out["forecast_weather_sign_window"] == out["sign_consistent_window"]
    assert out["forecast_weather_share"] == out["share_explained"]
    assert out["forecast_weather_sign"] == out["sign_consistent"]


def test_weather_outside_the_fits_training_range_is_not_inverted(
    db: duckdb.DuckDBPyConnection,
) -> None:
    # A heatwave far hotter than anything in training (Uri's cold, for a mild-history fit):
    # the model cannot say what the forecast priced in, so the climatology share stands in.
    load_store(db, heat_c=25.0, priced=True)
    out = _regress(db)
    assert out["forecast_weather_basis"] == "climatology"
    assert out["forecast_weather_share"] == out["share_explained"]


def test_h1_h2_h4_judge_weather_against_the_forecast() -> None:
    templates = {t.id: t for t in load_templates()}
    for hid in ("H1_WEATHER_DEMAND", "H2_SUPPLY_SHORTFALL", "H4_LOAD_SHED"):
        falsifier = templates[hid].falsifier
        assert "forecast_weather_share" in falsifier, hid
        assert "share_explained" not in falsifier, hid  # no climatology *share* anywhere
    assert (
        templates["H1_WEATHER_DEMAND"].explains == "regress.temperature.forecast_weather_error_mw"
    )
    # H4's load-shed signature (load against the temperature) and served-load test stay
    h4 = templates["H4_LOAD_SHED"].falsifier
    assert "sign_consistent == 1" in h4 and "implied_minus_actual_mean_z < 1" in h4


def test_forecast_relative_surprise_is_leak_free(db: duckdb.DuckDBPyConnection) -> None:
    load_store(db, heat_c=6.0, priced=False)
    before = run_tool(
        db,
        "regress.temperature",
        {"entity": "ERCO", "window": WINDOW, "event_start": EVENT[0], "event_end": EVENT[1]},
        AS_OF,
    )
    # Later information: revised forecasts and temperatures for the window, and new hours.
    later = AS_OF + timedelta(hours=6)
    rows = []
    for i in range(72):
        t = WINDOW[0] + timedelta(hours=i)
        rows.append(
            Observation(
                series_id="eia.DF",
                entity_id="ERCO",
                valid_time=t,
                value=99_999.0,
                observed_at=later,
                available_at=later,
                source="test",
            )
        )
        rows += [
            Observation(
                series_id="om.temp_2m",
                entity_id=z,
                valid_time=t,
                value=45.0,
                observed_at=later,
                available_at=later,
                source="test",
            )
            for z in ZONES
        ]
    append_observations(db, rows, dedupe_unchanged=False)
    after = run_tool(
        db,
        "regress.temperature",
        {"entity": "ERCO", "window": WINDOW, "event_start": EVENT[0], "event_end": EVENT[1]},
        AS_OF,
    )
    assert after.receipt_id == before.receipt_id
    assert after.output == before.output
