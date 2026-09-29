"""Detector calibration (docs/plans/2026-09-29_detector_calibration.md, Part A).

Requirements:
- The alarm threshold is calibrated per fit from the training residuals: k = the q-quantile of
  |z| over the leak-free training set (Monday refit, trailing 3 years), never below 3.
- It is leak-free: nothing after the refit date moves it.
- EIA placeholder hours (forecast error exactly 0: CAISO's first demand value is the forecast
  itself) are excluded from the threshold sample.
- An hour exceeds when |z| > k (exceedance > 1). The alarm needs >= 3 consecutive exceeding
  hours, or a run holding one hour at more than 2x the threshold.
- Knobs live in ontology/detector.yaml with per-BA overrides.
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
from hypothesis import given
from hypothesis import strategies as st

from gridcase.store.db import append_observations, connect
from gridcase.store.models import Observation
from gridcase.tools.anomaly import flag_runs
from gridcase.tools.base import run_tool
from gridcase.tools.detector import DetectorParams, detector_params, parse_detector_config

T0 = datetime(2023, 1, 2)  # a Monday
WEEKS = 12
AS_OF = T0 + timedelta(weeks=WEEKS - 1, days=3)  # Thursday; refit Monday = T0 + 11 weeks
REFIT = T0 + timedelta(weeks=WEEKS - 1)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "d.duckdb")
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


def _demand(t: datetime) -> float:
    weekend = 0.9 if t.weekday() >= 5 else 1.0
    return weekend * (40_000 + 8_000 * math.sin((t.hour - 6) / 24 * 2 * math.pi))


def load_noisy(
    db: duckdb.DuckDBPyConnection,
    entity: str = "ERCO",
    weeks: int = WEEKS,
    seed: int = 7,
    placeholders: set[datetime] | None = None,
) -> None:
    """Demand with a fat-tailed forecast error (Student-t, df=3, scale 500 MW)."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(24 * 7 * weeks):
        t = T0 + timedelta(hours=i)
        d = _demand(t)
        err = float(rng.standard_t(3)) * 500.0
        f = d if placeholders and t in placeholders else d - err
        rows += [_obs("eia.D", entity, t, d), _obs("eia.DF", entity, t, f)]
        if entity == "ERCO":
            for z in ("wz/austin", "wz/dallas", "wz/houston", "wz/san_antonio"):
                temp = 20 + 5 * math.sin((t.hour - 8) / 24 * 2 * math.pi)
                rows.append(_obs("om.temp_2m", z, t, temp))
    append_observations(db, rows, dedupe_unchanged=False)


def _detect(db: duckdb.DuckDBPyConnection, window: tuple[datetime, datetime]) -> dict[str, Any]:
    return run_tool(
        db,
        "anomaly.detect",
        {"metric": "forecast_error_mw", "entity": "ERCO", "window": window},
        AS_OF,
    ).output


LAST_WEEK = (REFIT, AS_OF)


# --------------------------------------------------------------------------- threshold fit


def test_threshold_is_calibrated_from_fat_tailed_training_residuals(
    db: duckdb.DuckDBPyConnection,
) -> None:
    load_noisy(db)
    out = _detect(db, LAST_WEEK)
    # Student-t(3) residuals put the 99.5th percentile of |z| well beyond 3 MAD-sigmas.
    assert out["threshold_z"] > 3.5
    assert out["threshold_n"] == (REFIT - T0).total_seconds() // 3600


def test_anomaly_quantile_threshold_is_leak_free(db: duckdb.DuckDBPyConnection) -> None:
    load_noisy(db)
    before = _detect(db, LAST_WEEK)["threshold_z"]
    # Huge misses after the refit Monday (visible at as_of) and after as_of (invisible):
    late = [REFIT + timedelta(hours=h) for h in range(1, 40)] + [
        AS_OF + timedelta(hours=h) for h in range(1, 40)
    ]
    append_observations(
        db,
        [_obs("eia.DF", "ERCO", t, _demand(t) - 60_000, lag_h=2) for t in late],
        dedupe_unchanged=False,
    )
    assert _detect(db, LAST_WEEK)["threshold_z"] == before
    # Control: the same misses *before* the refit date do move it.
    early = [T0 + timedelta(weeks=2, hours=h) for h in range(60)]
    append_observations(
        db,
        [_obs("eia.DF", "ERCO", t, _demand(t) - 60_000, lag_h=2) for t in early],
        dedupe_unchanged=False,
    )
    assert _detect(db, LAST_WEEK)["threshold_z"] > before


def test_placeholder_hours_are_excluded_from_the_threshold_fit(
    db: duckdb.DuckDBPyConnection,
) -> None:
    placeholders = {T0 + timedelta(days=d, hours=6) for d in range(0, 70, 2)}  # D == DF exactly
    load_noisy(db, placeholders=placeholders)
    out = _detect(db, LAST_WEEK)
    training_hours = (REFIT - T0).total_seconds() // 3600
    assert out["threshold_n"] == training_hours - len(placeholders)


def test_threshold_never_makes_the_detector_more_sensitive_than_three_sigma(
    db: duckdb.DuckDBPyConnection,
) -> None:
    # A perfect forecast: residuals ~0, so the empirical quantile would be ~0.
    rows = []
    for i in range(24 * 7 * WEEKS):
        t = T0 + timedelta(hours=i)
        rows += [_obs("eia.D", "ERCO", t, _demand(t)), _obs("eia.DF", "ERCO", t, _demand(t) - 1)]
    append_observations(db, rows, dedupe_unchanged=False)
    assert _detect(db, LAST_WEEK)["threshold_z"] == pytest.approx(3.0)


# --------------------------------------------------------------------------- alarm rule


def _hours(exc: list[float]) -> list[dict[str, object]]:
    return [{"t": T0 + timedelta(hours=i), "exceedance": e} for i, e in enumerate(exc)]


def _expected_flags(exc: list[float], min_run: int, single: float) -> set[int]:
    flagged: set[int] = set()
    i = 0
    while i < len(exc):
        if exc[i] > 1:
            j = i
            while j < len(exc) and exc[j] > 1:
                j += 1
            if j - i >= min_run or max(exc[i:j]) > single:
                flagged.update(range(i, j))
            i = j
        else:
            i += 1
    return flagged


@given(st.lists(st.floats(min_value=0.0, max_value=4.0), max_size=60))
def test_alarm_needs_three_hours_or_one_catastrophic_hour(exc: list[float]) -> None:
    runs = flag_runs(_hours(exc), min_run=3, single_hour_multiple=2.0)
    got = {int((t - T0).total_seconds() // 3600) for run in runs for t in run}
    assert got == _expected_flags(exc, 3, 2.0)


def test_alarm_rule_examples() -> None:
    def flagged(exc: list[float]) -> bool:
        return bool(flag_runs(_hours(exc), min_run=3, single_hour_multiple=2.0))

    assert not flagged([1.5, 1.9, 0.5])  # two moderate hours: no alarm
    assert flagged([1.2, 1.1, 1.3])  # three moderate hours: alarm
    assert flagged([0.2, 2.5, 0.1])  # one catastrophic hour: alarm
    assert not flagged([2.0, 0.0, 2.0])  # exactly 2x is not "more than" 2x


def test_a_single_hour_flags_only_when_catastrophic(db: duckdb.DuckDBPyConnection) -> None:
    load_noisy(db)
    base = _detect(db, LAST_WEEK)
    k = float(base["threshold_z"])
    by_t = {h["t"]: h for h in base["hours"]}

    def revise(t: datetime, exceedance: float) -> None:
        h = by_t[t.isoformat()]
        sigma = (h["actual"] - h["expected"]) / h["z"]
        err = h["expected"] + exceedance * k * sigma  # the forecast error we want at t
        append_observations(
            db, [_obs("eia.DF", "ERCO", t, _demand(t) - err, lag_h=2)], dedupe_unchanged=False
        )

    def hour(out: dict[str, Any], t: datetime) -> dict[str, Any]:
        return next(h for h in out["hours"] if h["t"] == t.isoformat())

    quiet = REFIT + timedelta(days=1, hours=14)
    for dt in (-1, 1):  # neighbours comfortably inside the threshold
        revise(quiet + timedelta(hours=dt), 0.2)
    revise(quiet, 1.5)
    moderate = _detect(db, LAST_WEEK)
    assert 1 < hour(moderate, quiet)["exceedance"] <= 2
    assert hour(moderate, quiet)["in_alarm"] is False

    loud = REFIT + timedelta(days=2, hours=14)
    for dt in (-1, 1):
        revise(loud + timedelta(hours=dt), 0.2)
    revise(loud, 3.0)
    catastrophic = _detect(db, LAST_WEEK)
    assert hour(catastrophic, loud)["exceedance"] > 2
    assert hour(catastrophic, loud)["in_alarm"] is True
    assert catastrophic["flagged"] is True


# --------------------------------------------------------------------------- config


def test_detector_config_defaults_match_the_plan() -> None:
    p = detector_params("ERCO")
    assert p == DetectorParams(
        quantile=0.995, min_run=3, single_hour_multiple=2.0, min_threshold_z=3.0
    )


def test_detector_config_allows_per_ba_overrides() -> None:
    cfg = parse_detector_config(
        {
            "defaults": {
                "quantile": 0.995,
                "min_run": 3,
                "single_hour_multiple": 2.0,
                "min_threshold_z": 3.0,
            },
            "overrides": {"CISO": {"quantile": 0.99, "min_run": 4}},
        }
    )
    assert cfg.for_entity("CISO").quantile == 0.99
    assert cfg.for_entity("CISO").min_run == 4
    assert cfg.for_entity("CISO").single_hour_multiple == 2.0
    assert cfg.for_entity("ERCO").quantile == 0.995


def test_detector_config_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        parse_detector_config({"defaults": {"quantile": 1.5, "min_run": 3}})
    with pytest.raises(ValueError):
        parse_detector_config(
            {"defaults": {"quantile": 0.995, "min_run": 0, "single_hour_multiple": 2.0}}
        )
