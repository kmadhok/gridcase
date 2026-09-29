"""Tool runner. A tool is `fn(ctx, inputs) -> (output, effect_size)`; everything it reads
goes through ctx.query (governed metric SQL) or ctx.raw (audited raw SQL), both logged into
the receipt with a hash of the rows they returned.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import duckdb
from pydantic import BaseModel, ValidationError

from gridcase.ledger.schema import Receipt
from gridcase.ledger.store import write_receipt
from gridcase.semantic.compiler import compile_query
from gridcase.semantic.schema import MetricQuery
from gridcase.tools.registry import TOOL_INPUTS

Rows = list[tuple[Any, ...]]
ToolFn = Callable[["ToolCtx", Any], tuple[dict[str, Any], float | None]]

TOOLS: dict[str, ToolFn] = {}
_IMPLS = [
    "gridcase.tools.baseline",
    "gridcase.tools.anomaly",
    "gridcase.tools.variance",
    "gridcase.tools.bridge",
    "gridcase.tools.contribution",
    "gridcase.tools.regression",
    "gridcase.tools.identity",
    "gridcase.tools.revision",
    "gridcase.tools.peers",
    "gridcase.tools.price",
]


class ToolError(ValueError):
    pass


def tool(name: str) -> Callable[[ToolFn], ToolFn]:
    def deco(fn: ToolFn) -> ToolFn:
        if name not in TOOL_INPUTS:
            raise RuntimeError(f"{name} is not in the registry")
        TOOLS[name] = fn
        return fn

    return deco


def _ensure_loaded() -> None:
    if len(TOOLS) < len(TOOL_INPUTS):
        for mod in _IMPLS:
            importlib.import_module(mod)


def rows_hash(rows: Rows) -> str:
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


@dataclass
class ToolCtx:
    conn: duckdb.DuckDBPyConnection
    as_of: datetime
    sql_log: list[dict[str, str]] = field(default_factory=list)
    series_used: set[str] = field(default_factory=set)
    t_min: datetime | None = None
    t_max: datetime | None = None

    def query(self, q: MetricQuery) -> Rows:
        if q.as_of != self.as_of:
            raise ToolError("tools must query at their own as_of")
        c = compile_query(q)
        rows = self.raw(c.sql)
        self.series_used.update(c.series_used)
        self._extend(q.window)
        return rows

    def metric(
        self, metric: str, entity: str, window: tuple[datetime, datetime], **kw: Any
    ) -> Rows:
        return self.query(
            MetricQuery(metric=metric, entity=entity, window=window, as_of=self.as_of, **kw)
        )

    def raw(self, sql: str) -> Rows:
        rows = [tuple(r) for r in self.conn.execute(sql).fetchall()]
        self.sql_log.append({"sql": sql, "rows_hash": rows_hash(rows)})
        return rows

    def _extend(self, window: tuple[datetime, datetime]) -> None:
        self.t_min = window[0] if self.t_min is None else min(self.t_min, window[0])
        self.t_max = window[1] if self.t_max is None else max(self.t_max, window[1])

    def data_version(self) -> datetime | None:
        if not self.series_used or self.t_min is None or self.t_max is None:
            return None
        series = ", ".join(f"'{s}'" for s in sorted(self.series_used))
        row = self.conn.execute(
            f"SELECT max(observed_at) FROM as_of(?::TIMESTAMP) WHERE series_id IN ({series}) "
            "AND valid_time >= ? AND valid_time < ?",
            [self.as_of, self.t_min, self.t_max],
        ).fetchone()
        return row[0] if row else None


def _round(obj: Any, nd: int = 6) -> Any:
    """Round floats so receipts survive last-bit numeric noise; NaN/inf become None.
    Six decimals keeps shares and ratios exact enough while absorbing 1-ulp differences."""
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_round(v, nd) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def run_tool(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    inputs: Mapping[str, Any] | BaseModel,
    as_of: datetime,
    case_id: str | None = None,
) -> Receipt:
    _ensure_loaded()
    if name not in TOOLS:
        raise ToolError(f"unknown tool {name!r}")
    model = TOOL_INPUTS[name]
    try:
        parsed = inputs if isinstance(inputs, model) else model.model_validate(dict(inputs))
    except ValidationError as e:
        raise ToolError(f"{name}: invalid inputs: {e.errors()[0]['msg']}") from e
    ctx = ToolCtx(conn=conn, as_of=as_of)
    t0 = time.perf_counter()
    output, effect = TOOLS[name](ctx, parsed)
    runtime_ms = int((time.perf_counter() - t0) * 1000)
    receipt = Receipt.build(
        tool=name,
        inputs=parsed.model_dump(mode="json"),
        as_of=as_of,
        output=_round(output),
        data_version=ctx.data_version(),
        effect_size=None if effect is None else round(float(effect), 6),
        runtime_ms=runtime_ms,
        sql=ctx.sql_log,
    )
    if case_id is not None:
        write_receipt(conn, case_id, receipt)
    return receipt


def expected_hours(window: tuple[datetime, datetime]) -> int:
    return int((window[1] - window[0]).total_seconds() // 3600)
