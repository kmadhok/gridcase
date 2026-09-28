"""The compiler turns a MetricQuery into self-contained SQL over as_of(). No LLM, no params."""

from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

from gridcase.semantic.compiler import CompileError, compile_query
from gridcase.semantic.schema import MetricQuery

WEEK = (datetime(2023, 5, 1), datetime(2023, 5, 8))
AS_OF = datetime(2023, 6, 1)


def q(metric: str, entity: str = "ERCO", grain: str = "hourly", **kw: object) -> MetricQuery:
    return MetricQuery(metric=metric, entity=entity, grain=grain, window=WEEK, as_of=AS_OF, **kw)  # type: ignore[arg-type]


def test_series_metric_scans_as_of_with_embedded_literals() -> None:
    sql = compile_query(q("demand")).sql
    assert "as_of(TIMESTAMP '2023-06-01 00:00:00')" in sql
    assert "series_id = 'eia.D'" in sql and "entity_id = 'ERCO'" in sql
    assert "valid_time >= TIMESTAMP '2023-05-01 00:00:00'" in sql
    assert "valid_time < TIMESTAMP '2023-05-08 00:00:00'" in sql
    assert "?" not in sql and "$" not in sql


def test_derived_metric_joins_dependencies_on_time(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    rows = fixture_store.execute(compile_query(q("balance_residual")).sql).fetchall()
    assert len(rows) == 24 * 7
    t, v = rows[0]
    assert t == datetime(2023, 5, 1)
    d = fixture_store.execute(compile_query(q("demand")).sql).fetchall()[0][1]
    ng = fixture_store.execute(compile_query(q("net_generation")).sql).fetchall()[0][1]
    ti = fixture_store.execute(compile_query(q("net_interchange")).sql).fetchall()[0][1]
    assert v == pytest.approx(d - (ng - ti))


def test_aggregate_metric_averages_the_bas_weather_zones(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    sql = compile_query(q("temp_load_center")).sql
    assert "'wz/houston'" in sql and "'wz/dallas'" in sql and "avg(" in sql.lower()
    rows = fixture_store.execute(sql).fetchall()
    assert len(rows) == 24 * 7
    assert 10 < rows[0][1] < 40  # Texas in May, degC


def test_fuel_dimension_groups_by_canonical_fuel(fixture_store: duckdb.DuckDBPyConnection) -> None:
    rows = fixture_store.execute(compile_query(q("generation_by_fuel")).sql).fetchall()
    fuels = {r[1] for r in rows}
    assert {"natural_gas", "wind", "solar", "coal", "nuclear"} <= fuels
    assert "NG" not in fuels and "WND" not in fuels
    first_hour = [r for r in rows if r[0] == datetime(2023, 5, 1)]
    assert len(first_hour) == len({r[1] for r in first_hour})  # one row per fuel per hour


def test_subregion_dimension_uses_sub_ba_series(fixture_store: duckdb.DuckDBPyConnection) -> None:
    rows = fixture_store.execute(compile_query(q("demand_by_subregion")).sql).fetchall()
    subs = {r[1] for r in rows}
    assert subs == {"COAS", "EAST", "FWES", "NCEN", "NRTH", "SCEN", "SOUT", "WEST"}


def test_daily_grain_sums_degree_days_and_averages_demand(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    hourly = fixture_store.execute(compile_query(q("hdd")).sql).fetchall()
    daily = fixture_store.execute(compile_query(q("hdd", grain="daily")).sql).fetchall()
    assert len(daily) == 7
    day1 = sum(v for t, v in hourly if t < datetime(2023, 5, 2))
    assert daily[0][1] == pytest.approx(day1)
    dd = fixture_store.execute(compile_query(q("demand", grain="daily")).sql).fetchall()
    dh = fixture_store.execute(compile_query(q("demand")).sql).fetchall()
    assert dd[0][1] == pytest.approx(sum(v for t, v in dh if t < datetime(2023, 5, 2)) / 24)


def test_capacity_factor_proxy_is_between_zero_and_one(
    fixture_store: duckdb.DuckDBPyConnection,
) -> None:
    rows = fixture_store.execute(compile_query(q("wind_capacity_factor_proxy")).sql).fetchall()
    assert len(rows) == 24 * 7
    assert all(0 <= v <= 1 for _, v in rows)
    assert max(v for _, v in rows) == pytest.approx(1.0)  # week max is the trailing max


def test_hub_metric_from_a_ba_resolves_to_its_hubs() -> None:
    sql = compile_query(q("lmp_da_hub")).sql
    assert "'ERCO/HB_HUBAVG'" in sql and "series_id = 'gs.lmp_da_hub'" in sql


def test_compiler_rejects_unknown_metric_entity_and_grain() -> None:
    with pytest.raises(CompileError, match="metric"):
        compile_query(q("nope"))
    with pytest.raises(CompileError, match="entity"):
        compile_query(q("demand", entity="MARS"))
    with pytest.raises(CompileError, match="grain"):
        compile_query(q("demand", grain="weekly"))


def test_compiled_query_reports_series_it_reads() -> None:
    c = compile_query(q("balance_residual"))
    assert c.series_used == ["eia.D", "eia.NG", "eia.TI"]
    assert c.metrics_used == ["balance_residual", "demand", "net_generation", "net_interchange"]
