"""The KPI alarm: a plain rolling-statistics rule on "demand vs. forecast (%)".

Alarm when the latest settled hours sit more than k standard deviations from the KPI's own
average over the previous N days, for at least `min_run` consecutive hours on the same side.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from gridcase.alarm import AlarmParams, check_kpi
from gridcase.store.db import append_observations, connect
from gridcase.store.models import Observation

NOW = datetime(2024, 3, 1, 12)
PARAMS = AlarmParams(lookback_days=28, k_sigma=3.0, min_run=3, recent_hours=24, settle_hours=0)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "a.duckdb")
    yield conn
    conn.close()


def _obs(series: str, entity: str, t: datetime, v: float, seen: datetime) -> Observation:
    return Observation(
        series_id=series,
        entity_id=entity,
        valid_time=t,
        value=v,
        observed_at=seen,
        available_at=seen,
        source="test",
    )


def load(
    db: duckdb.DuckDBPyConnection,
    entity: str = "ERCO",
    recent: dict[int, float] | None = None,
    days: int = 30,
) -> None:
    """Hourly D and DF up to NOW. Normal hours alternate +1% / -1% (mean 0, sd 1); `recent`
    maps hours-before-NOW (1 = the last hour) to an error in percent."""
    rows = []
    recent = recent or {}
    for k in range(1, days * 24 + 1):
        t = NOW - timedelta(hours=k)
        pct = recent.get(k, 1.0 if k % 2 else -1.0)
        forecast = 50_000.0
        rows.append(_obs("eia.DF", entity, t, forecast, t - timedelta(hours=20)))
        rows.append(_obs("eia.D", entity, t, forecast * (1 + pct / 100), t + timedelta(minutes=30)))
    append_observations(db, rows)


def test_three_hours_beyond_three_sigma_raise_an_alarm(db: duckdb.DuckDBPyConnection) -> None:
    load(db, recent={1: 5.0, 2: 5.0, 3: 5.0})
    r = check_kpi(db, "ERCO", NOW, PARAMS)
    o = r.output
    assert o["alarm"] is True and o["direction"] == "above"
    assert o["run_hours"] == 3
    assert o["run_start"] == (NOW - timedelta(hours=3)).isoformat()
    assert abs(o["mean"]) < 0.01 and abs(o["std"] - 1.0) < 0.01
    assert abs(o["worst_value"] - 5.0) < 1e-6 and o["worst_z"] > 3
    assert r.tool == "kpi.alarm" and r.sql  # the query that produced it is on the receipt


def test_two_hours_are_not_enough(db: duckdb.DuckDBPyConnection) -> None:
    load(db, recent={1: 5.0, 2: 5.0})
    assert check_kpi(db, "ERCO", NOW, PARAMS).output["alarm"] is False


def test_a_run_must_stay_on_one_side(db: duckdb.DuckDBPyConnection) -> None:
    load(db, recent={1: 5.0, 2: -5.0, 3: 5.0})
    assert check_kpi(db, "ERCO", NOW, PARAMS).output["alarm"] is False


def test_below_forecast_alarms_too(db: duckdb.DuckDBPyConnection) -> None:
    load(db, recent={1: -8.0, 2: -9.0, 3: -8.5, 4: -8.0})
    o = check_kpi(db, "ERCO", NOW, PARAMS).output
    assert o["alarm"] is True and o["direction"] == "below" and o["run_hours"] == 4
    assert abs(o["worst_value"] + 9.0) < 1e-6


def test_the_run_is_judged_against_history_before_the_recent_window(
    db: duckdb.DuckDBPyConnection,
) -> None:
    """A long event must not raise its own bar: the average and spread come from the
    `lookback_days` before the recent window, never from the hours being judged."""
    load(db, recent={k: 6.0 for k in range(1, 25)})
    o = check_kpi(db, "ERCO", NOW, PARAMS).output
    assert o["alarm"] is True and abs(o["std"] - 1.0) < 0.01


def test_data_published_after_as_of_is_invisible(db: duckdb.DuckDBPyConnection) -> None:
    load(db)
    late = [
        _obs("eia.D", "ERCO", NOW - timedelta(hours=k), 60_000.0, NOW + timedelta(hours=2))
        for k in (1, 2, 3)
    ]
    append_observations(db, late)
    before = check_kpi(db, "ERCO", NOW, PARAMS)
    assert before.output["alarm"] is False
    assert check_kpi(db, "ERCO", NOW + timedelta(hours=3), PARAMS).output["alarm"] is True


def test_placeholder_hours_where_demand_equals_forecast_are_ignored(
    db: duckdb.DuckDBPyConnection,
) -> None:
    """EIA's first CAISO demand value is the forecast itself (error exactly 0)."""
    load(db, recent={1: 5.0, 2: 0.0, 3: 5.0, 4: 5.0})
    o = check_kpi(db, "ERCO", NOW, PARAMS).output
    assert o["alarm"] is False  # the placeholder breaks the run rather than joining it
    assert o["n_placeholder"] == 1


def test_unsettled_hours_are_not_judged(db: duckdb.DuckDBPyConnection) -> None:
    """US48's forecast is a running sum for about a day: its newest hours are not judged."""
    load(db, entity="US48", recent={1: 60.0, 2: 60.0, 3: 60.0})
    p = AlarmParams(lookback_days=28, k_sigma=3.0, min_run=3, recent_hours=24, settle_hours=24)
    o = check_kpi(db, "US48", NOW, p).output
    assert o["alarm"] is False and o["judged_until"] == (NOW - timedelta(hours=24)).isoformat()


def test_same_inputs_give_the_same_receipt(db: duckdb.DuckDBPyConnection) -> None:
    load(db, recent={1: 5.0, 2: 5.0, 3: 5.0})
    assert (
        check_kpi(db, "ERCO", NOW, PARAMS).receipt_id
        == check_kpi(db, "ERCO", NOW, PARAMS).receipt_id
    )


def test_too_little_history_is_reported_not_alarmed(db: duckdb.DuckDBPyConnection) -> None:
    load(db, days=3, recent={1: 5.0, 2: 5.0, 3: 5.0})
    o = check_kpi(db, "ERCO", NOW, PARAMS).output
    assert o["alarm"] is False and o["insufficient"] is True


def test_defaults_come_from_the_alarm_config() -> None:
    from gridcase.alarm import alarm_params

    p = alarm_params("ERCO")
    assert (p.lookback_days, p.k_sigma, p.min_run) == (28, 3.0, 3)
    assert alarm_params("US48").settle_hours >= 24


# ------------------------------------------------------------------ on real history


def test_uri_raises_an_alarm_and_a_calm_week_does_not(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    from gridcase.alarm import alarm_params

    uri = check_kpi(fixture_store, "ERCO", datetime(2021, 2, 16, 12), alarm_params("ERCO")).output
    assert uri["alarm"] is True and uri["direction"] == "below"
    calm = check_kpi(fixture_store, "ERCO", datetime(2023, 5, 3, 12), alarm_params("ERCO")).output
    assert calm["alarm"] is False
