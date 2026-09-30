"""Semantic layer for MarketCase: KPIs and dimensions over Polymarket trades.

`ontology/kpis.yaml` defines measures (governed SQL aggregates), derived KPIs (arithmetic over
measures, parsed by `semantic.expr`) and dimensions (governed SQL expressions). A `KpiQuery`
asks for one KPI over a time window, optionally filtered to segments and broken down by one
dimension; `compile_kpi` assembles one self-contained SQL statement from those fragments with
every literal embedded (so a receipt can re-run it) and every value quoted. Data comes only
from `trades_as_of(as_of)`: nothing collected after as_of is visible.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from functools import cache
from typing import Any, Literal

import duckdb
import yaml
from pydantic import BaseModel, Field

from gridcase.config import ONTOLOGY_DIR
from gridcase.semantic.expr import Node, identifiers, parse_expr, to_sql

Grain = Literal["hour", "day", "week", "month", "total"]
FilterValue = str | list[str]
Filters = Mapping[str, FilterValue]


class KpiError(ValueError):
    pass


class Measure(BaseModel):
    id: str
    label: str
    description: str
    unit: str
    sql: str
    additive: bool
    internal: bool = False  # a building block of other KPIs, not listed in the catalog


class Derived(BaseModel):
    id: str
    label: str
    description: str
    unit: str
    expr: str
    weight: str  # the measure a segment's weight is counted in (trades for an average size)


class Share(BaseModel):
    """A slice's share of a measure: the query's filters (and group) pick the slice, the
    denominator is the same measure over everything in the time bucket."""

    id: str
    label: str
    description: str
    unit: str
    of: str


class Nested(BaseModel):
    """Aggregated in two steps: first per item (`per`: an outcome, a wallet, a market), then
    across items. `where` is a fixed, governed condition on the trades."""

    id: str
    label: str
    description: str
    unit: str
    per: str
    inner: dict[str, str]
    outer: str
    where: str = "TRUE"
    empty_is_zero: bool = False
    breakdown_as: str | None = None  # explain changes by breaking down this KPI instead


class Dimension(BaseModel):
    id: str
    label: str
    sql: str
    values: list[str] | None = None


Kpi = Measure | Derived | Share | Nested


class Limit(BaseModel):
    """A numeric condition on each trade, e.g. trade_usd >= 1000 or price < 0.1."""

    field: str
    op: Literal[">=", "<"]
    value: float


class KpiQuery(BaseModel):
    kpi: str
    grain: Grain = "hour"
    window: tuple[datetime, datetime]
    as_of: datetime
    filters: Filters = Field(default_factory=dict)
    limits: list[Limit] = Field(default_factory=list)
    group_by: str | None = None


class CompiledKpi(BaseModel):
    sql: str
    kpi: str
    measures: list[str]


@cache
def load_kpis() -> tuple[dict[str, Kpi], dict[str, Dimension]]:
    doc = yaml.safe_load((ONTOLOGY_DIR / "kpis.yaml").read_text())
    measures = set(doc["measures"])
    kpis: dict[str, Kpi] = {k: Measure(id=k, **v) for k, v in doc["measures"].items()}
    for k, v in doc["derived"].items():
        d = Derived(id=k, **v)
        unknown = identifiers(parse_expr(d.expr)) - measures
        if unknown:
            raise KpiError(f"{k} refers to unknown measures {sorted(unknown)}")
        kpis[k] = d
    for k, v in (doc.get("shares") or {}).items():
        if v["of"] not in measures:
            raise KpiError(f"{k} is a share of unknown measure {v['of']!r}")
        kpis[k] = Share(id=k, **v)
    for k, v in (doc.get("nested") or {}).items():
        kpis[k] = Nested(id=k, **v)
    for k in kpis:
        target = getattr(kpis[k], "breakdown_as", None)
        if target is not None and target not in kpis:
            raise KpiError(f"{k} breaks down as unknown KPI {target!r}")
    dims = {k: Dimension(id=k, **v) for k, v in doc["dimensions"].items()}
    return kpis, dims


@cache
def numeric_fields() -> dict[str, str]:
    """Governed numeric trade fields a limit may use, id -> column."""
    doc = yaml.safe_load((ONTOLOGY_DIR / "kpis.yaml").read_text())
    fields: dict[str, str] = doc.get("numeric") or {}
    return fields


def month_to_date(as_of: datetime) -> tuple[datetime, datetime]:
    return as_of.replace(day=1, hour=0, minute=0, second=0, microsecond=0), as_of


def _ts(t: datetime) -> str:
    return f"TIMESTAMP '{t:%Y-%m-%d %H:%M:%S}'"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _values(v: FilterValue) -> list[str]:
    return [v] if isinstance(v, str) else list(v)


def _validate(q: KpiQuery, kpis: dict[str, Kpi], dims: dict[str, Dimension]) -> None:
    if q.kpi not in kpis:
        raise KpiError(f"unknown KPI {q.kpi!r}")
    for dim, value in q.filters.items():
        if dim not in dims:
            raise KpiError(f"unknown dimension {dim!r}")
        values = _values(value)
        if not values:
            raise KpiError(f"{dim}: empty list of segments")
        allowed = dims[dim].values
        for v in values:
            if allowed is not None and v not in allowed:
                raise KpiError(f"{dim} has no segment {v!r} (known: {', '.join(allowed)})")
    for lim in q.limits:
        if lim.field not in numeric_fields():
            raise KpiError(f"unknown numeric field {lim.field!r}")
    if q.group_by is not None and q.group_by not in dims:
        raise KpiError(f"unknown dimension {q.group_by!r}")
    if q.window[1] <= q.window[0]:
        raise KpiError("window must end after it starts")


def _where(q: KpiQuery) -> str:
    parts = []
    for k, v in sorted(q.filters.items()):
        vals = sorted(_values(v))
        parts.append(
            f"d_{k} = {_quote(vals[0])}"
            if len(vals) == 1
            else f"d_{k} IN ({', '.join(_quote(x) for x in vals)})"
        )
    cols = numeric_fields()
    for lim in sorted(q.limits, key=lambda x: (x.field, x.op, x.value)):
        parts.append(f"{cols[lim.field]} {lim.op} {float(lim.value)!r}")
    return " AND ".join(parts) or "TRUE"


def _body(q: KpiQuery, kpi: Kpi, kpis: dict[str, Kpi]) -> tuple[str, list[str], list[str]]:
    """The KPI's aggregation over `e`: extra CTEs, the measures it uses, and the SQL it needs."""
    keys = [f"date_trunc('{q.grain}', ts) AS t"] if q.grain != "total" else []
    if q.group_by:
        keys.append(f"d_{q.group_by} AS segment")
    out = [k for k, on in (("t", q.grain != "total"), ("segment", bool(q.group_by))) if on]
    where = _where(q)
    select_keys = ", ".join([*out, ""])

    if isinstance(kpi, Nested):
        inner = ", ".join(f"{sql} AS {name}" for name, sql in kpi.inner.items())
        cte = f"""per_item AS (
    SELECT {", ".join([*keys, f"{kpi.per} AS item", inner])}
    FROM e WHERE ({kpi.where}) AND {where}
    GROUP BY ALL
),
agg AS (
    SELECT {select_keys}{kpi.outer} AS v FROM per_item GROUP BY ALL
)
SELECT {select_keys}v FROM agg ORDER BY ALL"""
        return cte, [kpi.id], [kpi.where, kpi.per, *kpi.inner.values(), kpi.outer]

    if isinstance(kpi, Share):
        m = kpis[kpi.of]
        assert isinstance(m, Measure)
        t_keys = keys[:1] if q.grain != "total" else []
        join = "JOIN den USING (t)" if q.grain != "total" else "CROSS JOIN den"
        cte = f"""num AS (
    SELECT {", ".join([*keys, f"{m.sql} AS n"])}
    FROM e WHERE {where}
    GROUP BY ALL
),
den AS (
    SELECT {", ".join([*t_keys, f"{m.sql} AS d"])} FROM e GROUP BY ALL
)
SELECT {select_keys}CAST(n AS DOUBLE) / nullif(d, 0) AS v FROM num {join} ORDER BY ALL"""
        return cte, [m.id], [m.sql]

    node: Node | None = parse_expr(kpi.expr) if isinstance(kpi, Derived) else None
    used = sorted(identifiers(node)) if node is not None else [kpi.id]
    measures = [m for m in (kpis[u] for u in used) if isinstance(m, Measure)]
    aggs = ", ".join(f"{m.sql} AS m_{m.id}" for m in measures)
    value = (
        to_sql(node, lambda name: f"CAST(m_{name} AS DOUBLE)")
        if node is not None
        else f"m_{kpi.id}"
    )
    cte = f"""agg AS (
    SELECT {", ".join([*keys, aggs])}
    FROM e WHERE {where}
    GROUP BY ALL
)
SELECT {select_keys}{value} AS v FROM agg ORDER BY ALL"""
    return cte, used, [m.sql for m in measures]


def compile_kpi(q: KpiQuery) -> CompiledKpi:
    kpis, dims = load_kpis()
    _validate(q, kpis, dims)
    kpi = kpis[q.kpi]
    body, used, sqls = _body(q, kpi, kpis)

    as_of, lo, hi = _ts(q.as_of), _ts(q.window[0]), _ts(q.window[1])
    dim_cols = ",\n               ".join(f"{d.sql} AS d_{d.id}" for d in dims.values())
    # a market's first trade in our record, only when a KPI asks for it
    new_markets = any("first_market_ts" in s for s in sqls)
    market_cte = (
        f"""
market_first AS (
    SELECT condition_id, min(ts) AS first_market_ts FROM trades_as_of({as_of})
    WHERE condition_id IN (SELECT DISTINCT condition_id FROM win) GROUP BY condition_id
),"""
        if new_markets
        else ""
    )
    market_col = ", market_first.first_market_ts" if new_markets else ""
    market_join = " JOIN market_first USING (condition_id)" if new_markets else ""
    sql = f"""WITH win AS (
    SELECT t.*, m.category AS m_category, m.event_title AS m_event_title
    FROM trades_as_of({as_of}) t LEFT JOIN markets m USING (condition_id)
    WHERE t.ts >= {lo} AND t.ts < {hi}
),
first_seen AS (
    SELECT wallet, min(ts) AS first_ts FROM trades_as_of({as_of})
    WHERE wallet IN (SELECT DISTINCT wallet FROM win) GROUP BY wallet
),{market_cte}
e AS (
    SELECT win.ts, win.wallet, win.usd, win.price, win.condition_id, win.asset{market_col},
               {dim_cols}
    FROM win JOIN first_seen USING (wallet){market_join}
),
{body}"""
    return CompiledKpi(sql=sql, kpi=q.kpi, measures=used)


def run_kpi(conn: duckdb.DuckDBPyConnection, q: KpiQuery) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute(compile_kpi(q).sql).fetchall()]


SEGMENT_SEP = ";"  # not '&': a category is called "Economy & Finance"
VALUE_SEP = "|"  # several values of one dimension


def segment_label(filters: Filters) -> str:
    """A case's `entity`: 'all', 'category=Sports' or 'category=Crypto|Sports;side=buy'."""
    return (
        SEGMENT_SEP.join(
            f"{k}={VALUE_SEP.join(sorted(_values(v)))}" for k, v in sorted(filters.items())
        )
        or "all"
    )


def parse_segment(label: str) -> dict[str, FilterValue]:
    if label in ("", "all"):
        return {}
    out: dict[str, FilterValue] = {}
    for part in label.split(SEGMENT_SEP):
        k, v = part.split("=", 1)
        vals = v.split(VALUE_SEP)
        out[k] = vals if len(vals) > 1 else v
    return out
