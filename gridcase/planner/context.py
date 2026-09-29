"""What the planner sees: the anomaly, the entity's ontology slice, applicable templates with
priors, similar closed cases, and the concrete windows/instants tools will need."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import duckdb
from pydantic import BaseModel, Field

from gridcase.config import REGIONS
from gridcase.planner.hypotheses import load_templates, season_of
from gridcase.semantic.compiler import compile_query
from gridcase.semantic.ontology import load_ontology
from gridcase.semantic.schema import MetricQuery
from gridcase.store.series import SERIES_BY_ID

WINDOW_HOURS = 72


class PlanContext(BaseModel):
    case_id: str
    entity: str
    metric: str
    as_of: datetime
    window: tuple[datetime, datetime]
    t0: datetime
    t1: datetime
    event_start: datetime  # first flagged hour of the case anomaly (window start if none)
    peers: list[str]
    hub: str | None
    season: str
    anomaly: dict[str, Any]
    applicable: list[str]
    priors: dict[str, float]
    similar_cases: list[dict[str, Any]] = Field(default_factory=list)


def fill_inputs(inputs: dict[str, Any], ctx: PlanContext) -> dict[str, Any]:
    values: dict[str, Any] = {
        "{entity}": ctx.entity,
        "{metric}": ctx.metric,
        "{window}": [ctx.window[0], ctx.window[1]],
        "{t0}": ctx.t0,
        "{t1}": ctx.t1,
        "{as_of}": ctx.as_of,
        "{as_of_minus_48h}": ctx.as_of - timedelta(hours=48),
        "{peers}": list(ctx.peers),
        "{hub}": ctx.hub,
        "{event_start}": ctx.event_start,
    }
    return {k: values.get(v, v) if isinstance(v, str) else v for k, v in inputs.items()}


def default_windows(as_of: datetime) -> tuple[tuple[datetime, datetime], datetime, datetime]:
    window = (as_of - timedelta(hours=WINDOW_HOURS), as_of)
    t1 = as_of - timedelta(hours=SERIES_BY_ID["eia.fuel"].lag_hours)
    return window, t1 - timedelta(hours=24), t1


def worst_hour_with_fuel_data(
    conn: duckdb.DuckDBPyConnection, entity: str, as_of: datetime, window: tuple[datetime, datetime]
) -> datetime | None:
    """The hour of largest supply gap (forecast minus generation) known at as_of. Served load
    tracks generation during load shed, so the gap, not the generation level, locates the
    shortfall; the bridge is anchored on it."""
    gap = compile_query(
        MetricQuery(metric="supply_gap_mw", entity=entity, window=window, as_of=as_of)
    )
    rows: list[tuple[datetime, float]] = [
        (t, float(v)) for t, v in conn.execute(gap.sql).fetchall() if v is not None
    ]
    if not rows:
        return None
    worst: datetime = max(rows, key=lambda r: (r[1], r[0]))[0]
    return worst


def build_context(
    conn: duckdb.DuckDBPyConnection,
    case_id: str,
    entity: str,
    metric: str,
    as_of: datetime,
    anomaly: dict[str, Any],
    priors: dict[str, float],
    similar_cases: list[dict[str, Any]] | None = None,
) -> PlanContext:
    onto = load_ontology()
    window, t0, t1 = default_windows(as_of)
    worst = worst_hour_with_fuel_data(conn, entity, as_of, window)
    if worst is not None:
        t1 = worst
        t0 = t1 - timedelta(hours=24)
    hubs = onto.hubs(entity)
    anchor = anomaly.get("event_start") or anomaly.get("first_flag_t")
    event_start = datetime.fromisoformat(anchor) if isinstance(anchor, str) else window[0]
    return PlanContext(
        case_id=case_id,
        entity=entity,
        metric=metric,
        as_of=as_of,
        window=window,
        t0=t0,
        t1=t1,
        event_start=event_start,
        peers=[r for r in REGIONS if r not in (entity, "US48")],
        hub=hubs[0] if hubs else None,
        season=season_of(as_of.date()),
        anomaly=anomaly,
        applicable=[t.id for t in load_templates()],
        priors=priors,
        similar_cases=similar_cases or [],
    )
