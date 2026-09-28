"""Tool registry: names and input schemas the planner may select. Bodies arrive in Phase 3.

Tool names are stable identifiers used by hypothesis templates and plans; adding one is a
governed change (docs/DECISIONS.md).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MetricEntityWindow(BaseModel):
    metric: str
    entity: str
    window: tuple[datetime, datetime]


class EntityWindow(BaseModel):
    entity: str
    window: tuple[datetime, datetime]


class BridgeInputs(BaseModel):
    entity: str
    t0: datetime
    t1: datetime


class RevisionInputs(BaseModel):
    series: str
    entity: str
    t_a: datetime
    t_b: datetime


class PeerInputs(BaseModel):
    entity: str
    peers: list[str] = Field(default_factory=list)
    window: tuple[datetime, datetime]


class PriceInputs(BaseModel):
    hub: str
    window: tuple[datetime, datetime]


TOOL_INPUTS: dict[str, type[BaseModel]] = {
    "baseline.expected": MetricEntityWindow,
    "anomaly.detect": MetricEntityWindow,
    "variance.plan_vs_actual": EntityWindow,
    "bridge.fuel": BridgeInputs,
    "contribution.subregion": EntityWindow,
    "regress.temperature": EntityWindow,
    "identity.balance": EntityWindow,
    "revision.diff": RevisionInputs,
    "peer.compare": PeerInputs,
    "price.co_movement": PriceInputs,
}

TOOL_NAMES: list[str] = sorted(TOOL_INPUTS)
