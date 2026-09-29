"""Fault injector: writes a synthetic scenario with a known cause into a scratch copy of the
store. Eight generators, one per hypothesis template; parameters sampled from
bench/params.yaml with numpy's default_rng(seed) so every case is reproducible.

Scratch stores are fabricated: values are edited in place. The production store is never
touched, and no injected row ever leaves the scratch file.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import yaml
from pydantic import BaseModel, Field

from gridcase.config import ROOT
from gridcase.semantic.ontology import load_ontology
from gridcase.store.db import connect
from gridcase.tools.variance import us_holidays

GENERATORS = [
    "H0_DATA_ARTIFACT",
    "H1_WEATHER_DEMAND",
    "H2_SUPPLY_SHORTFALL",
    "H3_IMPORT_CONSTRAINT",
    "H4_LOAD_SHED",
    "H5_SUBREGION_CONCENTRATION",
    "H6_RENEWABLE_FORECAST_MISS",
    "H7_CALENDAR",
]
GROUND_TRUTH = {
    "H0_DATA_ARTIFACT": ["H0_DATA_ARTIFACT"],
    "H1_WEATHER_DEMAND": ["H1_WEATHER_DEMAND"],
    "H2_SUPPLY_SHORTFALL": ["H2_SUPPLY_SHORTFALL", "H4_LOAD_SHED"],
    "H3_IMPORT_CONSTRAINT": ["H3_IMPORT_CONSTRAINT"],
    "H4_LOAD_SHED": ["H4_LOAD_SHED", "H2_SUPPLY_SHORTFALL"],
    "H5_SUBREGION_CONCENTRATION": ["H5_SUBREGION_CONCENTRATION"],
    "H6_RENEWABLE_FORECAST_MISS": ["H6_RENEWABLE_FORECAST_MISS"],
    "H7_CALENDAR": ["H7_CALENDAR"],
}
# Hours between the end of the fault and as_of, so the series the generator touches are
# knowable at as_of (NG/fuel/TI publish a day late, sub-BA later; demand and weather within
# the hour).
LEAD_HOURS = {
    "H0_DATA_ARTIFACT": 26,  # the identity check needs NG/TI for the damaged hours
    "H1_WEATHER_DEMAND": 2,
    "H2_SUPPLY_SHORTFALL": 26,
    "H3_IMPORT_CONSTRAINT": 26,
    "H4_LOAD_SHED": 26,
    "H5_SUBREGION_CONCENTRATION": 38,
    "H6_RENEWABLE_FORECAST_MISS": 26,
    "H7_CALENDAR": 2,
}
WINDOW_HOURS = 72
BASE_TEMP = 18.3
FUEL_CODES = {"natural_gas": "NG", "coal": "COL", "nuclear": "NUC", "wind": "WND"}
Window = tuple[datetime, datetime]


class Scenario(BaseModel):
    id: str
    generator: str | None
    seed: int
    entity: str
    metric: str
    as_of: datetime
    window: Window
    fault: Window | None
    params: dict[str, Any] = Field(default_factory=dict)
    ground_truth: list[str] = Field(default_factory=list)
    expect_flagged: bool = True


def load_params(path: Path | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((path or ROOT / "bench" / "params.yaml").read_text())
    return doc


def _rng(generator: str | None, seed: int) -> np.random.Generator:
    h = hashlib.sha256(f"{generator}:{seed}".encode()).digest()
    return np.random.default_rng(int.from_bytes(h[:8], "big"))


def _uniform(rng: np.random.Generator, lo_hi: list[float]) -> float:
    return float(rng.uniform(lo_hi[0], lo_hi[1]))


def _int(rng: np.random.Generator, lo_hi: list[int]) -> int:
    return int(rng.integers(lo_hi[0], lo_hi[1] + 1))


def _choice(rng: np.random.Generator, options: list[Any]) -> Any:
    return options[int(rng.integers(0, len(options)))]


def _midnight(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def _pick_as_of(rng: np.random.Generator, spans: list[list[date]]) -> datetime:
    span = _choice(rng, spans)
    start = _midnight(span[0]) + timedelta(hours=WINDOW_HOURS + 48)
    end = _midnight(span[1])
    hours = int((end - start).total_seconds() // 3600)
    return start + timedelta(hours=int(rng.integers(0, max(1, hours))))


def _holidays_in(spans: list[list[date]]) -> list[date]:
    out: list[date] = []
    for a, b in spans:
        for year in range(a.year, b.year + 1):
            out += [d for d in us_holidays(year) if a + timedelta(days=5) <= d < b]
    return sorted(out)


def sample_scenario(
    generator: str, seed: int, entity: str, params: dict[str, Any] | None = None
) -> Scenario:
    if generator not in GENERATORS:
        raise KeyError(generator)
    p = params or load_params()
    rng = _rng(generator, seed)
    spans = p["clean_spans"][entity]
    g = p["generators"][generator]
    sampled: dict[str, Any] = {}
    if generator == "H7_CALENDAR":
        day = _choice(rng, _holidays_in(spans))
        as_of = _midnight(day) + timedelta(days=1, hours=12)
        fault: Window = (_midnight(day), _midnight(day) + timedelta(days=1))
        sampled = {
            "holiday": day.isoformat(),
            "forecast_inflation": _uniform(rng, g["forecast_inflation"]),
        }
    else:
        as_of = _pick_as_of(rng, spans)
        hours = _int(rng, g["hours"])
        fault_end = as_of - timedelta(hours=LEAD_HOURS[generator])
        fault = (fault_end - timedelta(hours=hours), fault_end)
        sampled = {"hours": hours}
        if generator == "H0_DATA_ARTIFACT":
            sampled["kind"] = _choice(rng, g["kind"])
            sampled["outlier_factor"] = _uniform(rng, g["outlier_factor"])
        elif generator == "H1_WEATHER_DEMAND":
            sampled["delta_temp_c"] = _uniform(rng, g["delta_temp_c"])
            sampled["mw_per_degc"] = _uniform(rng, g["mw_per_degc"])
        elif generator == "H2_SUPPLY_SHORTFALL":
            sampled["fuel"] = _choice(rng, g["fuel"])
            sampled["loss_share"] = _uniform(rng, g["loss_share"])
        elif generator == "H3_IMPORT_CONSTRAINT":
            sampled["extra_import_mw"] = _uniform(rng, g["extra_import_mw"])
        elif generator == "H4_LOAD_SHED":
            sampled["shed_share"] = _uniform(rng, g["shed_share"])
        elif generator == "H5_SUBREGION_CONCENTRATION":
            sampled["subregion"] = _choice(rng, load_ontology().subregions(entity))
            sampled["scale"] = _uniform(rng, g["scale"])
        elif generator == "H6_RENEWABLE_FORECAST_MISS":
            sampled["loss_share"] = _uniform(rng, g["loss_share"])
    metric = (
        "wind_capacity_factor_proxy"
        if generator == "H6_RENEWABLE_FORECAST_MISS"
        else "forecast_error_mw"
    )
    return Scenario(
        id=f"{generator.split('_')[0].lower()}-{entity.lower()}-{seed}",
        generator=generator,
        seed=seed,
        entity=entity,
        metric=metric,
        as_of=as_of,
        window=(as_of - timedelta(hours=WINDOW_HOURS), as_of),
        fault=fault,
        params=sampled,
        ground_truth=list(GROUND_TRUTH[generator]),
    )


def sample_null(seed: int, entity: str, params: dict[str, Any] | None = None) -> Scenario:
    p = params or load_params()
    rng = _rng(None, seed)
    as_of = _pick_as_of(rng, p["clean_spans"][entity])
    return Scenario(
        id=f"null-{entity.lower()}-{seed}",
        generator=None,
        seed=seed,
        entity=entity,
        metric="forecast_error_mw",
        as_of=as_of,
        window=(as_of - timedelta(hours=WINDOW_HOURS), as_of),
        fault=None,
        expect_flagged=False,
    )


# --------------------------------------------------------------------------- scratch stores


def build_scratch(source_parquet: Path | None, dest: Path, source_db: Path | None = None) -> None:
    """A fresh store at dest with the source's observations (from a parquet snapshot or by
    attaching another store read-only)."""
    if dest.exists():
        dest.unlink()
    conn = connect(dest)
    if source_db is not None:
        conn.execute(f"ATTACH '{source_db}' AS src (READ_ONLY)")
        conn.execute("INSERT INTO observations SELECT * FROM src.observations")
        conn.execute("DETACH src")
    elif source_parquet is not None:
        conn.execute(
            "INSERT INTO observations SELECT * FROM read_parquet(?)", [str(source_parquet)]
        )
    else:
        raise ValueError("need source_parquet or source_db")
    conn.close()


def copy_scratch(base: Path, dest: Path) -> None:
    shutil.copyfile(base, dest)


def store_digest(conn: duckdb.DuckDBPyConnection, exclude_window: Window | None = None) -> str:
    where = ""
    params: list[Any] = []
    if exclude_window is not None:
        where = "WHERE NOT (valid_time >= ? AND valid_time < ?)"
        params = [exclude_window[0], exclude_window[1]]
    row = conn.execute(
        f"""
        SELECT md5(string_agg(series_id || '|' || entity_id || '|' || valid_time || '|' ||
                   coalesce(value::VARCHAR, 'null') || '|' || available_at, chr(10)
                   ORDER BY series_id, entity_id, valid_time, available_at))
        FROM observations {where}
        """,
        params,
    ).fetchone()
    return str(row[0]) if row else ""


# --------------------------------------------------------------------------- row helpers

Conn = duckdb.DuckDBPyConnection


def _values(conn: Conn, series: str, entity: str, window: Window) -> dict[datetime, float]:
    rows = conn.execute(
        "SELECT valid_time, value FROM observations WHERE series_id = ? AND entity_id = ? "
        "AND valid_time >= ? AND valid_time < ? ORDER BY valid_time",
        [series, entity, window[0], window[1]],
    ).fetchall()
    return {t: float(v) for t, v in rows if v is not None}


def _add(conn: Conn, series: str, entity: str, t: datetime, delta: float) -> None:
    conn.execute(
        "UPDATE observations SET value = value + ? WHERE series_id = ? AND entity_id = ? "
        "AND valid_time = ?",
        [delta, series, entity, t],
    )


def _set(conn: Conn, series: str, entity: str, t: datetime, value: float) -> None:
    conn.execute(
        "UPDATE observations SET value = ? WHERE series_id = ? AND entity_id = ? "
        "AND valid_time = ?",
        [value, series, entity, t],
    )


def _scale_window(conn: Conn, series: str, entity: str, fault: Window, factor: float) -> int:
    cur = conn.execute(
        "UPDATE observations SET value = value * ? WHERE series_id = ? AND entity_id = ? "
        "AND valid_time >= ? AND valid_time < ?",
        [factor, series, entity, fault[0], fault[1]],
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _scale_fuels(conn: Conn, entity: str, ratio_by_t: dict[datetime, float]) -> None:
    for t, ratio in ratio_by_t.items():
        conn.execute(
            "UPDATE observations SET value = value * ? WHERE series_id = 'eia.fuel' "
            "AND entity_id LIKE ? AND valid_time = ?",
            [ratio, f"{entity}/%", t],
        )


def _thermal(temp: float) -> float:
    return max(0.0, BASE_TEMP - temp) + max(0.0, temp - BASE_TEMP)


def _fault(sc: Scenario) -> Window:
    assert sc.fault is not None
    return sc.fault


# --------------------------------------------------------------------------- generators


def inject(conn: Conn, sc: Scenario) -> dict[str, Any]:
    """Apply the scenario's fault to the scratch store. Returns a short summary."""
    if sc.generator is None:
        return {"changed": 0}
    fn = {
        "H0_DATA_ARTIFACT": _inject_h0,
        "H1_WEATHER_DEMAND": _inject_h1,
        "H2_SUPPLY_SHORTFALL": _inject_h2,
        "H3_IMPORT_CONSTRAINT": _inject_h3,
        "H4_LOAD_SHED": _inject_h4,
        "H5_SUBREGION_CONCENTRATION": _inject_h5,
        "H6_RENEWABLE_FORECAST_MISS": _inject_h6,
        "H7_CALENDAR": _inject_h7,
    }[sc.generator]
    return fn(conn, sc)


def _inject_h0(conn: Conn, sc: Scenario) -> dict[str, Any]:
    fault = _fault(sc)
    if sc.params["kind"] == "drop_hours":
        cur = conn.execute(
            "DELETE FROM observations WHERE series_id = 'eia.D' AND entity_id = ? "
            "AND valid_time >= ? AND valid_time < ?",
            [sc.entity, fault[0], fault[1]],
        )
        row = cur.fetchone()
        return {"changed": int(row[0]) if row else 0, "kind": "drop_hours"}
    n = _scale_window(conn, "eia.D", sc.entity, fault, sc.params["outlier_factor"])
    return {"changed": n, "kind": "outliers"}


def _inject_h1(conn: Conn, sc: Scenario) -> dict[str, Any]:
    """Shift every load-centre temperature; demand (and generation, to keep the identity)
    follows with the sampled sensitivity; fuels scale with generation."""
    fault = _fault(sc)
    zones = load_ontology().weather_zones(sc.entity)
    delta, k = sc.params["delta_temp_c"], sc.params["mw_per_degc"]
    temps = _values(conn, "om.temp_2m", zones[0], fault)
    ng = _values(conn, "eia.NG", sc.entity, fault)
    df = _values(conn, "eia.DF", sc.entity, fault)
    # A weather miss worth scoring moves demand by at least 12% of the forecast. When the
    # sampled shift is too small for that, the shift itself grows, so temperature and demand
    # stay coupled through the sensitivity (a bigger surprise, not a decoupled response).
    mean_df = sum(df.values()) / len(df) if df else 0.0
    needed = 0.12 * mean_df / k
    if abs(delta) < needed:
        delta = needed if delta >= 0 else -needed
    for z in zones:
        for t in temps:
            _add(conn, "om.temp_2m", z, t, delta)
    ratios: dict[datetime, float] = {}
    for t, temp in temps.items():
        d_mw = k * (_thermal(temp + delta) - _thermal(temp))
        _add(conn, "eia.D", sc.entity, t, d_mw)
        _add(conn, "eia.NG", sc.entity, t, d_mw)
        if t in ng and ng[t]:
            ratios[t] = (ng[t] + d_mw) / ng[t]
    _scale_fuels(conn, sc.entity, ratios)
    return {"changed": len(temps), "delta_temp_c": delta}


def _inject_h2(conn: Conn, sc: Scenario) -> dict[str, Any]:
    """One fuel loses output; generation and served load fall by the same amount."""
    fault = _fault(sc)
    fuel_entity = f"{sc.entity}/{FUEL_CODES[sc.params['fuel']]}"
    levels = _values(conn, "eia.fuel", fuel_entity, fault)
    d = _values(conn, "eia.D", sc.entity, fault)
    share = sc.params["loss_share"]
    for t, v in levels.items():
        # a shortfall worth scoring is at least 12% of load, when the fleet has that much
        loss = max(share * v, min(0.12 * d.get(t, 0.0), 0.95 * v))
        _add(conn, "eia.fuel", fuel_entity, t, -loss)
        _add(conn, "eia.NG", sc.entity, t, -loss)
        _add(conn, "eia.D", sc.entity, t, -loss)
    return {"changed": len(levels), "fuel": sc.params["fuel"]}


def _inject_h3(conn: Conn, sc: Scenario) -> dict[str, Any]:
    """Interchange pinned past its deepest net import of the prior 90 days; the extra import
    serves extra demand (identity kept)."""
    fault = _fault(sc)
    hist = _values(conn, "eia.TI", sc.entity, (fault[0] - timedelta(days=90), fault[0]))
    if not hist:
        return {"changed": 0}
    pin = min(hist.values()) - sc.params["extra_import_mw"]
    ti = _values(conn, "eia.TI", sc.entity, fault)
    for t, v in ti.items():
        _set(conn, "eia.TI", sc.entity, t, pin)
        _add(conn, "eia.D", sc.entity, t, v - pin)
    return {"changed": len(ti), "pin_mw": pin}


def _inject_h4(conn: Conn, sc: Scenario) -> dict[str, Any]:
    """Served load capped below forecast; generation follows served load, fuels scale evenly."""
    fault = _fault(sc)
    d = _values(conn, "eia.D", sc.entity, fault)
    df = _values(conn, "eia.DF", sc.entity, fault)
    ng = _values(conn, "eia.NG", sc.entity, fault)
    share = sc.params["shed_share"]
    ratios: dict[datetime, float] = {}
    for t, v in d.items():
        cap = min(df.get(t, v), v) * (1 - share)
        shed = v - cap
        _set(conn, "eia.D", sc.entity, t, cap)
        if t in ng and ng[t]:
            _add(conn, "eia.NG", sc.entity, t, -shed)
            ratios[t] = max(0.0, (ng[t] - shed) / ng[t])
    _scale_fuels(conn, sc.entity, ratios)
    return {"changed": len(d), "shed_share": share}


def _inject_h5(conn: Conn, sc: Scenario) -> dict[str, Any]:
    """One subregion's demand scales up; the BA total and generation rise with it."""
    fault = _fault(sc)
    sub = f"{sc.entity}/{sc.params['subregion']}"
    levels = _values(conn, "eia.subba_demand", sub, fault)
    ng = _values(conn, "eia.NG", sc.entity, fault)
    d = _values(conn, "eia.D", sc.entity, fault)
    scale = sc.params["scale"]
    ratios: dict[datetime, float] = {}
    for t, v in levels.items():
        # a local miss worth scoring is material at BA level: at least 12% of BA demand
        extra = max(v * (scale - 1), 0.12 * d.get(t, 0.0))
        _add(conn, "eia.subba_demand", sub, t, extra)
        _add(conn, "eia.D", sc.entity, t, extra)
        _add(conn, "eia.NG", sc.entity, t, extra)
        if t in ng and ng[t]:
            ratios[t] = (ng[t] + extra) / ng[t]
    _scale_fuels(conn, sc.entity, ratios)
    return {"changed": len(levels), "subregion": sc.params["subregion"]}


def _inject_h6(conn: Conn, sc: Scenario) -> dict[str, Any]:
    """Wind output lost, made up by gas: totals unchanged, the mix moves."""
    fault = _fault(sc)
    wind, gas = f"{sc.entity}/WND", f"{sc.entity}/NG"
    levels = _values(conn, "eia.fuel", wind, fault)
    share = sc.params["loss_share"]
    for t, v in levels.items():
        _add(conn, "eia.fuel", wind, t, -share * v)
        _add(conn, "eia.fuel", gas, t, share * v)
    return {"changed": len(levels), "loss_share": share}


def _inject_h7(conn: Conn, sc: Scenario) -> dict[str, Any]:
    """The forecast for a holiday is shaped like an ordinary day."""
    n = _scale_window(conn, "eia.DF", sc.entity, _fault(sc), sc.params["forecast_inflation"])
    return {"changed": n, "holiday": sc.params["holiday"]}
