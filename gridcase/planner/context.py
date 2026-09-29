"""What the planner sees: the KPI and segment that moved, the window, how it compares with the
same hours on previous days, the drivers that apply, their priors, and similar past cases.
Template test inputs use placeholders filled here: {kpi} {filters} {window} {baseline_days}."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import duckdb
from pydantic import BaseModel, Field

from gridcase.planner.hypotheses import HypothesisTemplate, load_templates

BASELINE_DAYS = 7
DEFAULT_WINDOW_HOURS = 6


class PlanContext(BaseModel):
    case_id: str
    kpi: str
    filters: dict[str, str] = Field(default_factory=dict)
    as_of: datetime
    window: tuple[datetime, datetime]
    baseline_days: int = BASELINE_DAYS
    change: dict[str, Any] = Field(default_factory=dict)  # kpi.compare output for the window
    trigger: dict[str, Any] = Field(default_factory=dict)  # the alarm that opened the case
    record_days: int = 0
    applicable: list[str]
    priors: dict[str, float]
    similar_cases: list[dict[str, Any]] = Field(default_factory=list)


def fill_inputs(inputs: dict[str, Any], ctx: PlanContext) -> dict[str, Any]:
    values: dict[str, Any] = {
        "{kpi}": ctx.kpi,
        "{filters}": dict(ctx.filters),
        "{window}": [ctx.window[0], ctx.window[1]],
        "{baseline_days}": ctx.baseline_days,
        "{as_of}": ctx.as_of,
    }
    return {k: values.get(v, v) if isinstance(v, str) else v for k, v in inputs.items()}


def applies(t: HypothesisTemplate, filters: dict[str, str], record_days: int) -> bool:
    for pre in t.preconditions:
        kind, _, arg = pre.partition(":")
        if kind == "unfiltered" and arg in filters:
            return False
        if pre.startswith("record_days>=") and record_days < int(pre.split(">=")[1]):
            return False
    return True


def case_window(as_of: datetime, trigger: dict[str, Any] | None) -> tuple[datetime, datetime]:
    """The alarm's run of hours, or the last complete hours before as_of."""
    if trigger and trigger.get("run_start") and trigger.get("run_end"):
        lo = datetime.fromisoformat(trigger["run_start"])
        return lo, datetime.fromisoformat(trigger["run_end"]) + timedelta(hours=1)
    hi = as_of.replace(minute=0, second=0, microsecond=0)
    return hi - timedelta(hours=DEFAULT_WINDOW_HOURS), hi


def record_days(conn: duckdb.DuckDBPyConnection, as_of: datetime) -> int:
    row = conn.execute("SELECT min(ts) FROM trades_as_of(?)", [as_of]).fetchone()
    return 0 if not row or row[0] is None else (as_of - row[0]).days


def build_context(
    conn: duckdb.DuckDBPyConnection,
    case_id: str,
    kpi: str,
    filters: dict[str, str],
    as_of: datetime,
    window: tuple[datetime, datetime],
    change: dict[str, Any],
    priors: dict[str, float],
    trigger: dict[str, Any] | None = None,
    similar_cases: list[dict[str, Any]] | None = None,
) -> PlanContext:
    days = record_days(conn, as_of)
    return PlanContext(
        case_id=case_id,
        kpi=kpi,
        filters=filters,
        as_of=as_of,
        window=window,
        change=change,
        trigger=trigger or {},
        record_days=days,
        applicable=[t.id for t in load_templates() if applies(t, filters, days)],
        priors=priors,
        similar_cases=similar_cases or [],
    )
