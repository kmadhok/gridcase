"""Per-tool behaviour on tiny synthetic stores with hand-computable answers (DAMP)."""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from gridcase.store.db import append_observations, connect
from gridcase.store.models import Observation
from gridcase.tools.base import ToolError, run_tool

T0 = datetime(2023, 3, 1)  # a Wednesday
AS_OF = datetime(2023, 7, 1)  # after every synthetic window below (14 weeks from T0 = June 7)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "t.duckdb")
    yield conn
    conn.close()


def obs(
    series: str, entity: str, t: datetime, v: float, seen: datetime | None = None
) -> Observation:
    seen = seen or (t + timedelta(hours=1))
    return Observation(
        series_id=series,
        entity_id=entity,
        valid_time=t,
        value=v,
        observed_at=seen,
        available_at=seen,
        source="test",
    )


def hours(n: int, start: datetime = T0) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(n)]


def weekly_demand(t: datetime) -> float:
    """Smooth weekly pattern: 40 GW + daily sine + weekend dip."""
    h = t.hour + t.minute / 60
    weekend = 0.9 if t.weekday() >= 5 else 1.0
    return weekend * (40_000 + 8_000 * math.sin((h - 6) / 24 * 2 * math.pi))


def load_weeks(db: duckdb.DuckDBPyConnection, weeks: int, spike_at: datetime | None = None) -> None:
    rows = []
    for t in hours(24 * 7 * weeks):
        d = weekly_demand(t)
        if spike_at and spike_at <= t < spike_at + timedelta(hours=6):
            d -= 15_000
        rows += [
            obs("eia.D", "ERCO", t, d),
            obs("eia.DF", "ERCO", t, weekly_demand(t)),
            obs("eia.NG", "ERCO", t, d - 500),
            obs("eia.TI", "ERCO", t, -500),
        ]
        for z in ("wz/austin", "wz/dallas", "wz/houston", "wz/san_antonio"):
            rows.append(obs("om.temp_2m", z, t, 20 + 5 * math.sin((t.hour - 8) / 24 * 2 * math.pi)))
    append_observations(db, rows, dedupe_unchanged=False)


# --------------------------------------------------------------------------- baseline / anomaly


def test_baseline_learns_the_weekly_pattern(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 10)
    window = (T0 + timedelta(weeks=9), T0 + timedelta(weeks=10))
    out = run_tool(
        db, "baseline.expected", {"metric": "demand", "entity": "ERCO", "window": window}, AS_OF
    ).output
    assert len(out["hours"]) == 168
    for h in out["hours"]:
        t = datetime.fromisoformat(h["t"])
        assert h["expected"] == pytest.approx(weekly_demand(t), rel=0.02)
        assert h["sigma"] >= 0
    assert out["refit_date"] <= AS_OF.isoformat()


def test_baseline_refit_date_is_the_monday_before_as_of(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 4)
    window = (T0 + timedelta(weeks=3), T0 + timedelta(weeks=4))
    out = run_tool(
        db,
        "baseline.expected",
        {"metric": "demand", "entity": "ERCO", "window": window},
        datetime(2023, 6, 1, 15),
    ).output  # Thursday
    assert out["refit_date"] == "2023-05-29T00:00:00"


def test_baseline_never_trains_on_hours_after_refit(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 4)
    window = (T0 + timedelta(weeks=3), T0 + timedelta(weeks=4))
    as_of = T0 + timedelta(weeks=3, days=2)  # Friday 2023-03-24; refit Monday is 03-20
    out = run_tool(
        db, "baseline.expected", {"metric": "demand", "entity": "ERCO", "window": window}, as_of
    ).output
    refit = datetime(2023, 3, 20)
    assert out["refit_date"] == refit.isoformat()
    assert out["n_train"] == int(
        (refit - T0).total_seconds() // 3600
    )  # 456: nothing after the Monday


def test_anomaly_flags_a_six_hour_spike_and_only_that(db: duckdb.DuckDBPyConnection) -> None:
    spike = T0 + timedelta(weeks=9, days=2, hours=10)
    load_weeks(db, 10, spike_at=spike)
    window = (T0 + timedelta(weeks=9), T0 + timedelta(weeks=10))
    out = run_tool(
        db,
        "anomaly.detect",
        {"metric": "forecast_error_mw", "entity": "ERCO", "window": window},
        AS_OF,
    ).output
    assert out["flagged"] is True
    assert out["first_flag_t"] == spike.isoformat()
    assert out["n_flagged_hours"] == 6
    assert out["missing_hours"] == 0


def test_anomaly_needs_two_consecutive_hours(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 6)
    t = T0 + timedelta(weeks=5, days=1, hours=3)
    append_observations(
        db, [obs("eia.D", "ERCO", t, weekly_demand(t) - 20_000, seen=t + timedelta(hours=2))]
    )
    window = (T0 + timedelta(weeks=5), T0 + timedelta(weeks=6))
    out = run_tool(
        db,
        "anomaly.detect",
        {"metric": "forecast_error_mw", "entity": "ERCO", "window": window},
        AS_OF,
    ).output
    assert out["max_abs_z"] > 3 and out["flagged"] is False


def test_anomaly_counts_missing_hours(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 6)
    db.execute(
        "DELETE FROM observations WHERE series_id = 'eia.D' AND valid_time = ?",
        [T0 + timedelta(weeks=5, hours=7)],
    )
    window = (T0 + timedelta(weeks=5), T0 + timedelta(weeks=6))
    out = run_tool(
        db, "anomaly.detect", {"metric": "demand", "entity": "ERCO", "window": window}, AS_OF
    ).output
    assert out["missing_hours"] == 1


# --------------------------------------------------------------------------- variance / identity


def test_variance_reports_error_in_mw_and_pct_with_extremes(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 2, spike_at=T0 + timedelta(days=3, hours=12))
    window = (T0, T0 + timedelta(weeks=2))
    out = run_tool(
        db, "variance.plan_vs_actual", {"entity": "ERCO", "window": window}, AS_OF
    ).output
    assert out["n"] == 24 * 14
    assert out["min_error_mw"] == pytest.approx(-15_000)
    assert out["min_error_pct"] < -25
    # 2023-03-12 (DST spring-forward) falls inside the first two weeks of March
    assert out["calendar_events"] == ["dst:spring_forward"]
    assert out["calendar_event_in_window"] == 1


def test_variance_detects_a_holiday_and_dst_boundary(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(
        db,
        2,
    )
    thanksgiving = (datetime(2023, 11, 20), datetime(2023, 11, 27))
    dst = (datetime(2023, 3, 10), datetime(2023, 3, 14))
    from gridcase.tools.variance import calendar_events

    assert calendar_events(*thanksgiving) == ["holiday:thanksgiving"]
    assert calendar_events(*dst) == ["dst:spring_forward"]
    assert calendar_events(datetime(2023, 5, 1), datetime(2023, 5, 8)) == []


def test_identity_balance_z_is_small_when_books_close(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 14)
    window = (T0 + timedelta(weeks=13), T0 + timedelta(weeks=14))
    out = run_tool(db, "identity.balance", {"entity": "ERCO", "window": window}, AS_OF).output
    assert out["max_abs_z"] < 3
    assert out["n_missing"] == 0
    assert out["mean_residual_mw"] == pytest.approx(0.0, abs=1e-6)


def test_identity_balance_flags_a_broken_hour(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 14)
    t = T0 + timedelta(weeks=13, days=2, hours=5)
    append_observations(db, [obs("eia.NG", "ERCO", t, 10_000, seen=t + timedelta(hours=2))])
    window = (T0 + timedelta(weeks=13), T0 + timedelta(weeks=14))
    out = run_tool(db, "identity.balance", {"entity": "ERCO", "window": window}, AS_OF).output
    assert out["max_abs_z"] > 3
    assert out["worst_t"] == t.isoformat()


# --------------------------------------------------------------------------- bridge / contribution


def test_bridge_ranks_fuels_by_contribution_and_is_additive(db: duckdb.DuckDBPyConnection) -> None:
    t0, t1 = T0, T0 + timedelta(hours=24)
    for code, v0, v1 in [
        ("NG", 30_000, 20_000),
        ("WND", 8_000, 4_000),
        ("SUN", 1_000, 2_000),
        ("COL", 10_000, 10_000),
    ]:
        append_observations(
            db, [obs("eia.fuel", f"ERCO/{code}", t0, v0), obs("eia.fuel", f"ERCO/{code}", t1, v1)]
        )
    out = run_tool(db, "bridge.fuel", {"entity": "ERCO", "t0": t0, "t1": t1}, AS_OF).output
    assert out["total_change_mw"] == pytest.approx(-13_000)
    assert [f["fuel"] for f in out["fuels"]][:2] == ["natural_gas", "wind"]
    assert out["top_fuel"] == "natural_gas"
    # shares are of gross movement (10,000 + 4,000 + 1,000 + 0); receipts round to 6 decimals
    assert out["top_share"] == pytest.approx(10_000 / 15_000, abs=1e-6)
    assert out["top_delta_mw"] == pytest.approx(-10_000)
    assert out["renewable_share"] == pytest.approx(3_000 / 15_000, abs=1e-6)
    assert out["renewable_delta_mw"] == pytest.approx(-3_000)
    assert out["total_change_pct"] == pytest.approx(-13_000 / 49_000, abs=1e-6)


def test_bridge_expected_mode_measures_shortfall_against_each_fuels_baseline(
    db: duckdb.DuckDBPyConnection,
) -> None:
    # Ten weeks of a flat fuel mix, then gas loses 9,000 MW on the last day; nothing else moves.
    rows = []
    for t in hours(24 * 70):
        gas = 30_000.0 - (9_000.0 if t >= T0 + timedelta(days=69) else 0.0)
        rows += [
            obs("eia.fuel", "ERCO/NG", t, gas),
            obs("eia.fuel", "ERCO/WND", t, 8_000.0),
            obs("eia.fuel", "ERCO/COL", t, 10_000.0),
        ]
        for z in ("wz/austin", "wz/dallas", "wz/houston", "wz/san_antonio"):
            rows.append(obs("om.temp_2m", z, t, 20.0))
    append_observations(db, rows, dedupe_unchanged=False)
    t1 = T0 + timedelta(days=69, hours=12)
    # as_of on the day of the drop: the refit Monday precedes it, so the baseline is clean
    as_of = t1 + timedelta(hours=2)
    out = run_tool(
        db,
        "bridge.fuel",
        {"entity": "ERCO", "t0": t1 - timedelta(hours=24), "t1": t1, "mode": "expected"},
        as_of,
    ).output
    assert out["mode"] == "expected" and out["unmodelled_fuels"] == []
    assert out["top_fuel"] == "natural_gas"
    assert out["top_delta_mw"] == pytest.approx(-9_000, abs=50)
    assert out["top_share"] > 0.95
    # level mode against the same hour a day earlier sees the same drop here, but would not
    # if generation had been tracking served load (the Uri case); that is the point of the mode
    lvl = run_tool(
        db, "bridge.fuel", {"entity": "ERCO", "t0": t1 - timedelta(hours=24), "t1": t1}, AS_OF
    ).output
    assert lvl["top_delta_mw"] == pytest.approx(-9_000)


def test_bridge_with_no_data_raises_tool_error(db: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ToolError, match="no fuel data"):
        run_tool(
            db, "bridge.fuel", {"entity": "ERCO", "t0": T0, "t1": T0 + timedelta(hours=1)}, AS_OF
        )


def test_contribution_shares_change_vs_previous_week_additively(
    db: duckdb.DuckDBPyConnection,
) -> None:
    rows = []
    for t in hours(24 * 14):
        second_week = t >= T0 + timedelta(weeks=1)
        rows.append(obs("eia.subba_demand", "ERCO/COAS", t, 10_000 + (3_000 if second_week else 0)))
        rows.append(obs("eia.subba_demand", "ERCO/EAST", t, 5_000 + (1_000 if second_week else 0)))
        rows.append(obs("eia.subba_demand", "ERCO/WEST", t, 2_000))
    append_observations(db, rows, dedupe_unchanged=False)
    window = (T0 + timedelta(weeks=1), T0 + timedelta(weeks=2))
    out = run_tool(db, "contribution.subregion", {"entity": "ERCO", "window": window}, AS_OF).output
    # levels: COAS +3000 and EAST +1000 of a +4000 change
    assert out["level_shares"]["COAS"] == pytest.approx(0.75)
    assert out["level_shares"]["EAST"] == pytest.approx(0.25)
    assert out["total_change_mw"] == pytest.approx(4_000)
    # redistribution: only COAS gained share of BA load (17 GW -> 21 GW), so it takes all of it
    assert out["shares"]["COAS"] == pytest.approx(1.0)
    assert out["shares"]["EAST"] == pytest.approx(0.0) and out["shares"]["WEST"] == pytest.approx(
        0.0
    )
    assert out["top_subregion"] == "COAS" and out["top_share"] == pytest.approx(1.0)
    assert out["material"] is True


def test_contribution_ignores_uniform_weather_growth(db: duckdb.DuckDBPyConnection) -> None:
    rows = []
    for t in hours(24 * 14):
        f = 1.1 if t >= T0 + timedelta(weeks=1) else 1.0  # everything up 10%: weather, not local
        rows.append(obs("eia.subba_demand", "ERCO/COAS", t, 10_000 * f))
        rows.append(obs("eia.subba_demand", "ERCO/EAST", t, 5_000 * f))
        rows.append(obs("eia.subba_demand", "ERCO/WEST", t, 2_000 * f))
    append_observations(db, rows, dedupe_unchanged=False)
    window = (T0 + timedelta(weeks=1), T0 + timedelta(weeks=2))
    out = run_tool(db, "contribution.subregion", {"entity": "ERCO", "window": window}, AS_OF).output
    assert out["material"] is False and out["top_share"] == 0.0
    assert out["level_shares"]["COAS"] == pytest.approx(10 / 17)


# ------------------------------------------------- regression / peers / revisions / price


def test_regression_recovers_a_linear_temperature_slope(db: duckdb.DuckDBPyConnection) -> None:
    rows = []
    for i, t in enumerate(hours(24 * 35)):
        temp = 25 + 10 * math.sin(i / 24 * 2 * math.pi) + (i % 7)  # varied, > 18.3 mostly
        for z in ("wz/austin", "wz/dallas", "wz/houston", "wz/san_antonio"):
            rows.append(obs("om.temp_2m", z, t, temp))
        rows.append(obs("eia.D", "ERCO", t, 30_000 + 1_500 * max(0.0, temp - 18.3)))
        rows.append(obs("eia.DF", "ERCO", t, 30_000 + 1_500 * max(0.0, temp - 18.3)))
    append_observations(db, rows, dedupe_unchanged=False)
    window = (T0 + timedelta(weeks=4), T0 + timedelta(weeks=5))
    out = run_tool(db, "regress.temperature", {"entity": "ERCO", "window": window}, AS_OF).output
    assert out["r2"] > 0.95
    assert out["slope_mw_per_degc"] == pytest.approx(1_500, rel=0.05)
    assert out["implied_minus_actual_mw"] == pytest.approx(0, abs=200)


def test_regression_sign_consistency_separates_weather_misses_from_load_shed(
    db: duckdb.DuckDBPyConnection,
) -> None:
    # Two weeks at a steady 25 C, then a hot week the forecast did not expect: demand rises
    # with the heat (consistent). Then the mirror case: heat arrives but served load falls.
    def build(served_falls: bool) -> None:
        db.execute("DELETE FROM observations")
        rows = []
        for i, t in enumerate(hours(24 * 21)):
            hot = t >= T0 + timedelta(days=14)
            temp = (34.0 + 3 * math.sin(i / 24 * 2 * math.pi)) if hot else 25.0
            for z in ("wz/austin", "wz/dallas", "wz/houston", "wz/san_antonio"):
                rows.append(obs("om.temp_2m", z, t, temp))
            forecast = 30_000 + 1_500 * max(0.0, 25.0 - 18.3)
            actual = 30_000 + 1_500 * max(0.0, temp - 18.3)
            if hot and served_falls:  # hotter -> even less served load: the wrong sign
                actual = forecast - 8_000 - 500 * (temp - 25.0)
            rows += [obs("eia.D", "ERCO", t, actual), obs("eia.DF", "ERCO", t, forecast)]
        append_observations(db, rows, dedupe_unchanged=False)

    window = (T0 + timedelta(days=14), T0 + timedelta(days=21))
    build(served_falls=False)
    out = run_tool(db, "regress.temperature", {"entity": "ERCO", "window": window}, AS_OF).output
    assert out["sign_consistent"] == 1 and out["share_explained"] > 0.9
    build(served_falls=True)
    out = run_tool(db, "regress.temperature", {"entity": "ERCO", "window": window}, AS_OF).output
    assert out["sign_consistent"] == 0


def test_peer_compare_reports_correlation_per_peer(db: duckdb.DuckDBPyConnection) -> None:
    rows = []
    for i, t in enumerate(hours(48)):
        err = math.sin(i / 5)
        rows += [obs("eia.D", "ERCO", t, 40_000 + 1_000 * err), obs("eia.DF", "ERCO", t, 40_000)]
        rows += [obs("eia.D", "PJM", t, 90_000 + 3_000 * err), obs("eia.DF", "PJM", t, 90_000)]
        rows += [obs("eia.D", "CISO", t, 25_000 - 800 * err), obs("eia.DF", "CISO", t, 25_000)]
    append_observations(db, rows, dedupe_unchanged=False)
    out = run_tool(
        db,
        "peer.compare",
        {"entity": "ERCO", "peers": ["PJM", "CISO"], "window": (T0, T0 + timedelta(hours=48))},
        AS_OF,
    ).output
    assert out["corr"]["PJM"] == pytest.approx(1.0, abs=1e-6)
    assert out["corr"]["CISO"] == pytest.approx(-1.0, abs=1e-6)
    assert out["max_corr"] == pytest.approx(1.0, abs=1e-6)


def test_revision_diff_finds_changed_rows_between_two_vintages(
    db: duckdb.DuckDBPyConnection,
) -> None:
    t = T0 + timedelta(hours=5)
    append_observations(db, [obs("eia.D", "ERCO", h, 40_000) for h in hours(24)])
    later = T0 + timedelta(days=2)
    append_observations(db, [obs("eia.D", "ERCO", t, 44_000, seen=later)])
    out = run_tool(
        db,
        "revision.diff",
        {"series": "eia.D", "entity": "ERCO", "t_a": T0 + timedelta(days=1), "t_b": later},
        later,
    ).output
    assert out["n_changed"] == 1
    assert out["max_rel_change"] == pytest.approx(0.1)
    assert out["changed"][0]["valid_time"] == t.isoformat()
    assert out["history"] == "observed"


def test_revision_diff_reports_synthetic_only_history(db: duckdb.DuckDBPyConnection) -> None:
    rows = [
        Observation(
            series_id="eia.D",
            entity_id="ERCO",
            valid_time=h,
            value=1.0,
            observed_at=AS_OF,
            available_at=h + timedelta(hours=1),
            source="test",
            vintage="synthetic",
        )
        for h in hours(24)
    ]
    append_observations(db, rows, dedupe_unchanged=False)
    out = run_tool(
        db,
        "revision.diff",
        {"series": "eia.D", "entity": "ERCO", "t_a": T0 + timedelta(hours=12), "t_b": AS_OF},
        AS_OF,
    ).output
    assert out["n_changed"] == 0 and out["history"] == "synthetic_only"


def test_price_co_movement_without_data_says_so(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 1)
    out = run_tool(
        db,
        "price.co_movement",
        {"hub": "ERCO/HB_HUBAVG", "window": (T0, T0 + timedelta(days=7))},
        AS_OF,
    ).output
    assert out["has_data"] is False and out["cap_hours"] == 0 and out["corr"] is None


def test_price_co_movement_counts_cap_hours_and_correlates(db: duckdb.DuckDBPyConnection) -> None:
    load_weeks(db, 1, spike_at=T0 + timedelta(days=2, hours=12))
    rows = []
    for t in hours(24 * 7):
        price = (
            4_900.0
            if T0 + timedelta(days=2, hours=12) <= t < T0 + timedelta(days=2, hours=18)
            else 30.0
        )
        rows.append(obs("gs.lmp_da_hub", "ERCO/HB_HUBAVG", t, price))
    append_observations(db, rows, dedupe_unchanged=False)
    out = run_tool(
        db,
        "price.co_movement",
        {"hub": "ERCO/HB_HUBAVG", "window": (T0, T0 + timedelta(days=7))},
        AS_OF,
    ).output
    assert out["has_data"] is True and out["cap_hours"] == 6
    assert out["corr"] < -0.9  # price up when demand fell below forecast


# --------------------------------------------------------------------------- run_tool contract


def test_run_tool_validates_inputs_and_rejects_unknown_tools(db: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ToolError, match="unknown tool"):
        run_tool(db, "nope.tool", {}, AS_OF)
    with pytest.raises(ToolError, match="inputs"):
        run_tool(db, "identity.balance", {"entity": "ERCO"}, AS_OF)


def test_run_tool_writes_to_the_ledger_when_given_a_case(db: duckdb.DuckDBPyConnection) -> None:
    from gridcase.ledger.store import read_receipts

    load_weeks(db, 2)
    r = run_tool(
        db,
        "identity.balance",
        {"entity": "ERCO", "window": (T0, T0 + timedelta(days=7))},
        AS_OF,
        case_id="c1",
    )
    assert [x.receipt_id for x in read_receipts(db, "c1")] == [r.receipt_id]


def test_an_unexpected_exception_inside_a_tool_becomes_a_tool_error(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    """A bug or bad input deep in a tool must fail that step, not the whole investigation."""
    from gridcase.tools.base import TOOLS, ToolError, run_tool
    from gridcase.tools.registry import TOOL_INPUTS, EntityWindow

    def broken(ctx: object, inputs: object) -> tuple[dict[str, object], float | None]:
        raise TypeError("can't compare offset-naive and offset-aware datetimes")

    TOOLS["test.broken"] = broken
    TOOL_INPUTS["test.broken"] = EntityWindow
    try:
        with pytest.raises(ToolError, match="test.broken.*offset-naive"):
            run_tool(
                fixture_store,
                "test.broken",
                {"entity": "ERCO", "window": (datetime(2023, 5, 1), datetime(2023, 5, 2))},
                datetime(2023, 5, 3),
            )
    finally:
        del TOOLS["test.broken"]
        del TOOL_INPUTS["test.broken"]
