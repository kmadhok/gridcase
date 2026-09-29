"""Semantic layer for MarketCase: KPIs and dimensions over Polymarket trades.

`ontology/kpis.yaml` defines measures (governed SQL aggregates), derived KPIs (arithmetic over
measures, parsed by `semantic.expr`) and dimensions (governed SQL expressions). A `KpiQuery`
asks for one KPI over a time window, optionally filtered to segments and broken down by one
dimension; `compile_kpi` assembles one self-contained SQL statement from those fragments with
every literal embedded (so a receipt can re-run it) and every value quoted. Data comes only
from `trades_as_of(as_of)`: nothing collected after as_of is visible.
"""

from __future__ import annotations

from datetime import datetime
from functools import cache
from typing import Any, Literal

import duckdb
import yaml
from pydantic import BaseModel, Field

from gridcase.config import ONTOLOGY_DIR
from gridcase.semantic.expr import Node, identifiers, parse_expr, to_sql

Grain = Literal["hour", "day", "total"]


class KpiError(ValueError):
    pass


class Measure(BaseModel):
    id: str
    label: str
    description: str
    unit: str
    sql: str
    additive: bool


class Derived(BaseModel):
    id: str
    label: str
    description: str
    unit: str
    expr: str
    weight: str  # the measure a segment's weight is counted in (trades for an average size)


class Dimension(BaseModel):
    id: str
    label: str
    sql: str
    values: list[str] | None = None


Kpi = Measure | Derived


class KpiQuery(BaseModel):
    kpi: str
    grain: Grain = "hour"
    window: tuple[datetime, datetime]
    as_of: datetime
    filters: dict[str, str] = Field(default_factory=dict)
    group_by: str | None = None


class CompiledKpi(BaseModel):
    sql: str
    kpi: str
    measures: list[str]


@cache
def load_kpis() -> tuple[dict[str, Kpi], dict[str, Dimension]]:
    doc = yaml.safe_load((ONTOLOGY_DIR / "kpis.yaml").read_text())
    kpis: dict[str, Kpi] = {k: Measure(id=k, **v) for k, v in doc["measures"].items()}
    for k, v in doc["derived"].items():
        d = Derived(id=k, **v)
        unknown = identifiers(parse_expr(d.expr)) - set(doc["measures"])
        if unknown:
            raise KpiError(f"{k} refers to unknown measures {sorted(unknown)}")
        kpis[k] = d
    dims = {k: Dimension(id=k, **v) for k, v in doc["dimensions"].items()}
    return kpis, dims


def _ts(t: datetime) -> str:
    return f"TIMESTAMP '{t:%Y-%m-%d %H:%M:%S}'"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def compile_kpi(q: KpiQuery) -> CompiledKpi:
    kpis, dims = load_kpis()
    if q.kpi not in kpis:
        raise KpiError(f"unknown KPI {q.kpi!r}")
    for dim, value in q.filters.items():
        if dim not in dims:
            raise KpiError(f"unknown dimension {dim!r}")
        allowed = dims[dim].values
        if allowed is not None and value not in allowed:
            raise KpiError(f"{dim} has no segment {value!r} (known: {', '.join(allowed)})")
    if q.group_by is not None and q.group_by not in dims:
        raise KpiError(f"unknown dimension {q.group_by!r}")
    if q.window[1] <= q.window[0]:
        raise KpiError("window must end after it starts")

    kpi = kpis[q.kpi]
    node: Node | None = parse_expr(kpi.expr) if isinstance(kpi, Derived) else None
    used = sorted(identifiers(node)) if node is not None else [kpi.id]
    measures = {m: kpis[m] for m in used}

    as_of, lo, hi = _ts(q.as_of), _ts(q.window[0]), _ts(q.window[1])
    dim_cols = ",\n               ".join(f"{d.sql} AS d_{d.id}" for d in dims.values())
    where = " AND ".join(f"d_{k} = {_quote(v)}" for k, v in sorted(q.filters.items())) or "TRUE"
    keys = []
    if q.grain != "total":
        keys.append(f"date_trunc('{q.grain}', ts) AS t")
    if q.group_by:
        keys.append(f"d_{q.group_by} AS segment")
    aggs = ", ".join(f"{m.sql} AS m_{m.id}" for m in measures.values() if isinstance(m, Measure))
    value = (
        to_sql(node, lambda name: f"CAST(m_{name} AS DOUBLE)")
        if node is not None
        else f"m_{kpi.id}"
    )
    out_keys = [k for k, on in (("t", q.grain != "total"), ("segment", bool(q.group_by))) if on]
    sql = f"""WITH win AS (
    SELECT t.*, m.category AS m_category, m.event_title AS m_event_title
    FROM trades_as_of({as_of}) t LEFT JOIN markets m USING (condition_id)
    WHERE t.ts >= {lo} AND t.ts < {hi}
),
first_seen AS (
    SELECT wallet, min(ts) AS first_ts FROM trades_as_of({as_of})
    WHERE wallet IN (SELECT DISTINCT wallet FROM win) GROUP BY wallet
),
e AS (
    SELECT win.ts, win.wallet, win.usd, win.price,
               {dim_cols}
    FROM win JOIN first_seen USING (wallet)
),
agg AS (
    SELECT {", ".join([*keys, aggs])}
    FROM e WHERE {where}
    GROUP BY ALL
)
SELECT {", ".join([*out_keys, f"{value} AS v"])} FROM agg ORDER BY ALL"""
    return CompiledKpi(sql=sql, kpi=q.kpi, measures=list(measures))


def run_kpi(conn: duckdb.DuckDBPyConnection, q: KpiQuery) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute(compile_kpi(q).sql).fetchall()]


def segment_label(filters: dict[str, str]) -> str:
    """A case's `entity`: 'all', or 'category=Sports' (dimensions sorted, joined by '&')."""
    return "&".join(f"{k}={v}" for k, v in sorted(filters.items())) or "all"


def parse_segment(label: str) -> dict[str, str]:
    if label in ("", "all"):
        return {}
    return dict(part.split("=", 1) for part in label.split("&"))
