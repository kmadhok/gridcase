"""Each fault generator produces the signature its own hypothesis template looks for, on a
scratch copy of the fixture store, and leaves the rest of the store untouched."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest

from gridcase.bench.inject import (
    GENERATORS,
    Scenario,
    build_scratch,
    inject,
    sample_scenario,
    store_digest,
)
from gridcase.tools.base import run_tool
from tests.conftest import FIXTURE_PARQUET

ENTITY = "ERCO"


@pytest.fixture()
def scratch(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    path = tmp_path / "scratch.duckdb"
    build_scratch(FIXTURE_PARQUET, path)
    conn = duckdb.connect(str(path))
    conn.execute("SET threads = 1")
    yield conn
    conn.close()


def _inject(conn: duckdb.DuckDBPyConnection, generator: str, seed: int = 3) -> Scenario:
    sc = sample_scenario(generator, seed=seed, entity=ENTITY)
    inject(conn, sc)
    return sc


def test_generators_cover_all_eight_templates_and_sample_deterministically() -> None:
    assert len(GENERATORS) == 8
    a = sample_scenario("H1_WEATHER_DEMAND", seed=5, entity=ENTITY)
    b = sample_scenario("H1_WEATHER_DEMAND", seed=5, entity=ENTITY)
    assert a == b and a.ground_truth[0] == "H1_WEATHER_DEMAND"
    assert 2022 <= a.as_of.year <= 2023 and a.window[1] == a.as_of  # inside the clean span
    assert sample_scenario("H1_WEATHER_DEMAND", seed=6, entity=ENTITY).params != a.params


def test_injection_only_touches_the_window(scratch: duckdb.DuckDBPyConnection) -> None:
    sc = sample_scenario("H1_WEATHER_DEMAND", seed=3, entity=ENTITY)
    before = store_digest(scratch)
    before_outside = store_digest(scratch, exclude_window=sc.window)
    inject(scratch, sc)
    assert store_digest(scratch) != before
    assert store_digest(scratch, exclude_window=sc.window) == before_outside


def test_h0_missing_hours_and_outliers_break_the_identity(
    scratch: duckdb.DuckDBPyConnection,
) -> None:
    sc = _inject(scratch, "H0_DATA_ARTIFACT")
    out = run_tool(
        scratch, "identity.balance", {"entity": ENTITY, "window": sc.window}, sc.as_of
    ).output
    assert out["n_missing"] > 0 or out["max_abs_z"] > 3


def test_h1_weather_shift_moves_demand_with_the_right_sign(
    scratch: duckdb.DuckDBPyConnection,
) -> None:
    sc = _inject(scratch, "H1_WEATHER_DEMAND")
    out = run_tool(
        scratch, "regress.temperature", {"entity": ENTITY, "window": sc.window}, sc.as_of
    ).output
    assert out["sign_consistent"] == 1 and out["share_explained"] > 0.3


def test_h2_fuel_loss_opens_a_supply_gap_and_starves_served_load(
    scratch: duckdb.DuckDBPyConnection,
) -> None:
    sc = _inject(scratch, "H2_SUPPLY_SHORTFALL")
    gap = run_tool(
        scratch,
        "anomaly.detect",
        {"metric": "supply_gap_mw", "entity": ENTITY, "window": sc.window},
        sc.as_of,
    ).output
    assert gap["flagged"] is True
    reg = run_tool(
        scratch, "regress.temperature", {"entity": ENTITY, "window": sc.window}, sc.as_of
    ).output
    assert reg["implied_minus_actual_mw"] > 2000
    ident = run_tool(
        scratch, "identity.balance", {"entity": ENTITY, "window": sc.window}, sc.as_of
    ).output
    assert ident["max_abs_z"] < 3  # the fault respects D = NG - TI


def test_h3_interchange_clamped_at_its_limit(scratch: duckdb.DuckDBPyConnection) -> None:
    sc = _inject(scratch, "H3_IMPORT_CONSTRAINT")
    out = run_tool(
        scratch,
        "anomaly.detect",
        {"metric": "net_interchange", "entity": ENTITY, "window": sc.window},
        sc.as_of,
    ).output
    assert out["max_abs_z"] >= 2 or out["share_at_limit"] >= 0.2


def test_h4_served_load_capped_below_implied(scratch: duckdb.DuckDBPyConnection) -> None:
    sc = _inject(scratch, "H4_LOAD_SHED")
    var = run_tool(
        scratch, "variance.plan_vs_actual", {"entity": ENTITY, "window": sc.window}, sc.as_of
    ).output
    reg = run_tool(
        scratch, "regress.temperature", {"entity": ENTITY, "window": sc.window}, sc.as_of
    ).output
    assert var["min_error_pct"] <= -5 and reg["implied_minus_actual_mw"] >= 2000


def test_h5_one_subregion_carries_the_change(scratch: duckdb.DuckDBPyConnection) -> None:
    sc = _inject(scratch, "H5_SUBREGION_CONCENTRATION")
    assert sc.fault is not None
    # the planner passes the anomaly's event start; the tool then compares the event hours
    out = run_tool(
        scratch,
        "contribution.subregion",
        {"entity": ENTITY, "window": sc.window, "event_start": sc.fault[0]},
        sc.as_of,
    ).output
    assert out["top_share"] >= 0.6, out


def test_h6_wind_suppressed_and_bridge_led_by_renewables(
    scratch: duckdb.DuckDBPyConnection,
) -> None:
    sc = sample_scenario("H6_RENEWABLE_FORECAST_MISS", seed=3, entity=ENTITY)
    assert sc.fault is not None and sc.metric == "wind_capacity_factor_proxy"
    wind_sum = (
        "SELECT sum(value) FROM observations WHERE series_id = 'eia.fuel' "
        "AND entity_id = 'ERCO/WND' AND valid_time >= ? AND valid_time < ?"
    )
    before = scratch.execute(wind_sum, [sc.fault[0], sc.fault[1]]).fetchone()
    inject(scratch, sc)
    after = scratch.execute(wind_sum, [sc.fault[0], sc.fault[1]]).fetchone()
    assert before is not None and after is not None
    assert after[0] == pytest.approx(before[0] * (1 - sc.params["loss_share"]), rel=1e-6)
    # the expected-mode bridge sees renewables well below expectation at the fault's end
    out = run_tool(
        scratch,
        "bridge.fuel",
        {
            "entity": ENTITY,
            "t0": sc.fault[0],
            "t1": sc.fault[1] - timedelta(hours=1),
            "mode": "expected",
        },
        sc.as_of,
    ).output
    assert out["renewable_delta_mw"] < -3000
    # the wind-speed proxy is a weak model of fleet output (DECISIONS, Phase 5): the proxy
    # anomaly is reported, not required
    z = run_tool(
        scratch,
        "anomaly.detect",
        {"metric": "wind_capacity_factor_proxy", "entity": ENTITY, "window": sc.window},
        sc.as_of,
    ).output
    assert z["max_abs_z"] > 1.0


def test_h7_holiday_removed_from_forecast(scratch: duckdb.DuckDBPyConnection) -> None:
    sc = _inject(scratch, "H7_CALENDAR")
    out = run_tool(
        scratch, "variance.plan_vs_actual", {"entity": ENTITY, "window": sc.window}, sc.as_of
    ).output
    assert out["calendar_event_in_window"] == 1 and out["min_error_pct"] < -3


def test_unknown_generator_is_rejected() -> None:
    with pytest.raises(KeyError):
        sample_scenario("H9_NOPE", seed=1, entity=ENTITY)


def test_scratch_store_is_a_real_store_with_the_as_of_macro(
    scratch: duckdb.DuckDBPyConnection,
) -> None:
    n = scratch.execute("SELECT count(*) FROM as_of(TIMESTAMP '2023-06-01')").fetchone()
    assert n is not None and n[0] > 100_000
    assert scratch.execute("SELECT count(*) FROM cases").fetchone() == (0,)
