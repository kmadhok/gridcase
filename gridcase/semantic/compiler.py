"""MetricQuery -> SQL over the as_of view.

Three stages: resolve (metric + ontology -> a list of scan/derive nodes in dependency
order), plan (entity expansion: BA -> fuel entities, subregions, weather zones, hubs), emit
(CTEs with every literal embedded, so a receipt's SQL re-executes on its own). Deterministic:
sorted entity lists, post-order CTEs, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from gridcase.semantic import expr as E
from gridcase.semantic.ontology import Ontology, load_ontology
from gridcase.semantic.registry import load_metrics
from gridcase.semantic.schema import CompiledQuery, MetricDef, MetricQuery


class CompileError(ValueError):
    pass


def _ts(t: datetime) -> str:
    return f"TIMESTAMP '{t:%Y-%m-%d %H:%M:%S}'"


def _in(values: list[str]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")" if values else "('__none__')"


@dataclass
class _Ctx:
    q: MetricQuery
    metrics: dict[str, MetricDef]
    onto: Ontology
    ctes: list[tuple[str, str]] = field(default_factory=list)
    done: set[str] = field(default_factory=set)
    series_used: set[str] = field(default_factory=set)
    metrics_used: set[str] = field(default_factory=set)

    def add(self, name: str, body: str) -> None:
        self.ctes.append((name, body))
        self.done.add(name)


def _scan(
    ctx: _Ctx, m: MetricDef, window: tuple[datetime, datetime], dim_filter: dict[str, str]
) -> str:
    """SQL body for a series/aggregate metric over the window. Returns columns t[, dim], v."""
    assert m.series
    ctx.series_used.add(m.series)
    ba = ctx.q.entity
    base = (
        f"FROM as_of({_ts(ctx.q.as_of)})\n"
        f"    WHERE series_id = '{m.series}'\n"
        f"      AND valid_time >= {_ts(window[0])} AND valid_time < {_ts(window[1])}"
    )
    if m.kind == "aggregate":
        ents = ctx.onto.hubs(ba) if m.entity_kind == "hub" else ctx.onto.weather_zones(ba)
        # ORDER BY inside the aggregate pins the floating-point summation order.
        fn = "avg" if m.agg == "mean" else "sum"
        return (
            f"SELECT valid_time AS t, {fn}(value ORDER BY entity_id) AS v\n    {base}\n"
            f"      AND entity_id IN {_in(ents)}\n    GROUP BY 1"
        )
    if "fuel" in m.dims:
        emap = ctx.onto.fuel_entity_map(ba)
        if "fuel" in dim_filter:
            emap = {k: v for k, v in emap.items() if v == dim_filter["fuel"]}
        case = (
            "CASE entity_id " + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in emap.items()) + " END"
        )
        return (
            f"SELECT valid_time AS t, {case} AS fuel, sum(value ORDER BY entity_id) AS v\n"
            f"    {base}\n      AND entity_id IN {_in(list(emap))}\n    GROUP BY 1, 2"
        )
    if "subregion" in m.dims:
        ents = [f"{ba}/{s}" for s in ctx.onto.subregions(ba)]
        if "subregion" in dim_filter:
            ents = [e for e in ents if e.endswith("/" + dim_filter["subregion"])]
        return (
            f"SELECT valid_time AS t, split_part(entity_id, '/', 2) AS subregion, value AS v\n"
            f"    {base}\n      AND entity_id IN {_in(ents)}"
        )
    return f"SELECT valid_time AS t, value AS v\n    {base}\n      AND entity_id = '{ba}'"


def _resolve(ctx: _Ctx, mid: str) -> None:
    """Emit the CTE for metric `mid` (and its dependencies first)."""
    if mid in ctx.done:
        return
    m = ctx.metrics[mid]
    ctx.metrics_used.add(mid)
    if m.kind in ("series", "aggregate"):
        ctx.add(f"m_{mid}", _scan(ctx, m, ctx.q.window, ctx.q.dims if mid == ctx.q.metric else {}))
        return
    if m.kind == "rolling_ratio":
        assert m.numerator and m.trailing_hours
        num = ctx.metrics[m.numerator]
        ctx.metrics_used.add(m.numerator)
        ext = (ctx.q.window[0] - timedelta(hours=m.trailing_hours), ctx.q.window[1])
        ctx.add(f"m_{mid}_base", _scan(ctx, num, ext, m.numerator_dims))
        ctx.add(
            f"m_{mid}",
            "SELECT t, v / NULLIF(max(v) OVER (ORDER BY t RANGE BETWEEN INTERVAL "
            f"{m.trailing_hours} HOUR PRECEDING AND CURRENT ROW), 0) AS v\n    FROM m_{mid}_base",
        )
        return
    # derived
    deps = m.dependencies()
    for d in deps:
        if ctx.metrics[d].dims:
            raise CompileError(f"{mid}: derived metrics cannot depend on dimensioned metric {d}")
        _resolve(ctx, d)
    alias = {d: f"d{i}" for i, d in enumerate(deps)}
    assert m.expr
    sql_expr = E.to_sql(E.parse_expr(m.expr), lambda name: f"{alias[name]}.v")
    joins = f"FROM m_{deps[0]} d0" + "".join(
        f"\n    JOIN m_{d} {alias[d]} USING (t)" for d in deps[1:]
    )
    ctx.add(f"m_{mid}", f"SELECT d0.t, {sql_expr} AS v\n    {joins}")


def compile_query(q: MetricQuery) -> CompiledQuery:
    metrics, onto = load_metrics(), load_ontology()
    if q.metric not in metrics:
        raise CompileError(f"unknown metric {q.metric!r}")
    m = metrics[q.metric]
    if q.entity not in onto.bas:
        raise CompileError(f"unknown entity {q.entity!r}")
    if q.grain not in m.grains:
        raise CompileError(f"grain {q.grain!r} not allowed for {q.metric}")
    if q.window[1] <= q.window[0]:
        raise CompileError("window end must be after start")
    for d in q.dims:
        if d not in m.dims:
            raise CompileError(f"{q.metric} has no dimension {d!r}")

    ctx = _Ctx(q=q, metrics=metrics, onto=onto)
    _resolve(ctx, q.metric)
    dims = list(m.dims)
    dim_cols = "".join(f", {d}" for d in dims)
    final = f"m_{q.metric}"
    if q.grain == "daily":
        fn = "avg" if m.daily_agg == "mean" else "sum"
        group = ", ".join(str(i + 1) for i in range(1 + len(dims)))
        select = (
            f"SELECT date_trunc('day', t) AS t{dim_cols}, {fn}(v ORDER BY t) AS v\nFROM {final}\n"
            f"WHERE t >= {_ts(q.window[0])}\nGROUP BY {group}\nORDER BY {group}"
        )
    else:
        order = ", ".join(["t", *dims])
        select = (
            f"SELECT t{dim_cols}, v\nFROM {final}\nWHERE t >= {_ts(q.window[0])}\nORDER BY {order}"
        )
    with_clause = "WITH " + ",\n".join(f"{name} AS (\n    {body}\n)" for name, body in ctx.ctes)
    return CompiledQuery(
        sql=f"{with_clause}\n{select}",
        columns=["t", *dims, "v"],
        metrics_used=sorted(ctx.metrics_used),
        series_used=sorted(ctx.series_used),
    )
