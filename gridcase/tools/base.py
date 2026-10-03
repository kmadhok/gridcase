"""Tool runner. A tool is `fn(ctx, inputs) -> (output, effect_size)`; everything it reads
goes through ctx.kpi (governed KPI SQL from the semantic layer) or ctx.raw (audited raw SQL),
both logged into the receipt with a hash of the rows they returned.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import duckdb
from pydantic import BaseModel, ValidationError

from gridcase.ledger.schema import Receipt
from gridcase.ledger.store import write_receipt
from gridcase.semantic.kpis import KpiQuery, compile_kpi
from gridcase.tools.registry import ALL_INPUTS
from gridcase.trace import active_tracer

Rows = list[tuple[Any, ...]]
ToolFn = Callable[["ToolCtx", Any], tuple[dict[str, Any], float | None]]

TOOLS: dict[str, ToolFn] = {}
_IMPLS = [
    "gridcase.tools.compare",
    "gridcase.tools.breakdown",
    "gridcase.tools.coverage",
    "gridcase.tools.digest",
]
HOUR = timedelta(hours=1)


class ToolError(ValueError):
    pass


def tool(name: str) -> Callable[[ToolFn], ToolFn]:
    def deco(fn: ToolFn) -> ToolFn:
        if name not in ALL_INPUTS:
            raise RuntimeError(f"{name} is not in the registry")
        TOOLS[name] = fn
        return fn

    return deco


def _ensure_loaded() -> None:
    if len(TOOLS) < len(ALL_INPUTS):
        for mod in _IMPLS:
            importlib.import_module(mod)


def rows_hash(rows: Rows) -> str:
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


@dataclass
class ToolCtx:
    conn: duckdb.DuckDBPyConnection
    as_of: datetime
    sql_log: list[dict[str, str]] = field(default_factory=list)

    def kpi(self, q: KpiQuery) -> Rows:
        if q.as_of != self.as_of:
            raise ToolError("tools must query at their own as_of")
        return self.raw(compile_kpi(q).sql)

    def raw(self, sql: str, params: list[Any] | None = None) -> Rows:
        rows = [tuple(r) for r in self.conn.execute(sql, params or []).fetchall()]
        self.sql_log.append({"sql": sql, "rows_hash": rows_hash(rows)})
        return rows

    def record_start(self) -> datetime | None:
        """First complete hour of our record as known at as_of (collection began mid-hour)."""
        row = self.conn.execute("SELECT min(ts) FROM trades_as_of(?)", [self.as_of]).fetchone()
        first: datetime | None = row[0] if row else None
        if first is None:
            return None
        hour = first.replace(minute=0, second=0, microsecond=0)
        return hour if hour == first else hour + HOUR

    def gap_overlaps(self, lo: datetime, hi: datetime) -> bool:
        row = self.conn.execute(
            "SELECT count(*) FROM collector_gaps WHERE gap_to > ? AND gap_from < ? "
            "AND coalesce(detected_at, gap_to) <= ?",
            [lo, hi, self.as_of],
        ).fetchone()
        return bool(row and row[0])

    def baseline_windows(
        self, window: tuple[datetime, datetime], days: int
    ) -> list[tuple[datetime, datetime]]:
        """The same hours on each of the previous `days` days that our record fully covers."""
        start = self.record_start()
        out = []
        for d in range(1, days + 1):
            lo, hi = window[0] - timedelta(days=d), window[1] - timedelta(days=d)
            if start is not None and lo >= start and not self.gap_overlaps(lo, hi):
                out.append((lo, hi))
        return out

    def data_version(self) -> datetime | None:
        """When the newest trade we could see at as_of was collected."""
        row = self.conn.execute(
            "SELECT max(collected_at) FROM trades_as_of(?)", [self.as_of]
        ).fetchone()
        newest: datetime | None = row[0] if row else None
        return newest


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
    tracer = active_tracer.get()
    if tracer is None:
        return _run_tool(conn, name, inputs, as_of, case_id)
    raw = inputs.model_dump(mode="json") if isinstance(inputs, BaseModel) else dict(inputs)
    with tracer.span("tool", name, inputs=raw) as detail:
        receipt = _run_tool(conn, name, inputs, as_of, case_id)
        detail["receipt_id"] = receipt.receipt_id
        return receipt


def _run_tool(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    inputs: Mapping[str, Any] | BaseModel,
    as_of: datetime,
    case_id: str | None,
) -> Receipt:
    _ensure_loaded()
    if name not in TOOLS:
        raise ToolError(f"unknown tool {name!r}")
    model = ALL_INPUTS[name]
    try:
        parsed = inputs if isinstance(inputs, model) else model.model_validate(dict(inputs))
    except ValidationError as e:
        raise ToolError(f"{name}: invalid inputs: {e.errors()[0]['msg']}") from e
    ctx = ToolCtx(conn=conn, as_of=as_of)
    t0 = time.perf_counter()
    try:
        output, effect = TOOLS[name](ctx, parsed)
    except ToolError:
        raise
    except Exception as e:  # a bug or bad input deep in a tool fails this step only
        raise ToolError(f"{name} failed: {type(e).__name__}: {e}") from e
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
