"""Tool registry: names and input shapes the planner may use. Adding a tool is a governed
change (docs/DECISIONS.md). Every tool reads trades only through the semantic layer
(gridcase/semantic/kpis.py) at its own as_of."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from gridcase.semantic.kpis import FilterValue, Limit


class KpiWindow(BaseModel):
    kpi: str
    filters: dict[str, FilterValue] = Field(default_factory=dict)
    limits: list[Limit] = Field(default_factory=list)
    window: tuple[datetime, datetime]
    # compared with the same hours on each of this many previous days
    baseline_days: int = Field(default=7, ge=1, le=28)


class Breakdown(KpiWindow):
    top_n: int = Field(default=5, ge=1, le=20)


class CoverageInputs(BaseModel):
    window: tuple[datetime, datetime]


# one breakdown tool per dimension: the falsifier language reads `tool.name.field`, so two
# breakdowns under one name would overwrite each other's outputs
BREAKDOWNS: dict[str, str] = {
    "by.market": "market",
    "by.category": "category",
    "by.size": "size_band",
    "by.trader": "trader_type",
    "by.odds": "odds_band",
    "by.kind": "market_kind",
}

TOOL_INPUTS: dict[str, type[BaseModel]] = {
    "kpi.compare": KpiWindow,
    "data.coverage": CoverageInputs,
    **{name: Breakdown for name in BREAKDOWNS},
}

TOOL_NAMES: list[str] = sorted(TOOL_INPUTS)
