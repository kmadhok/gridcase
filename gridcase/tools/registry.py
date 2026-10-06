"""Tool registry: names and input shapes the planner may use. Adding a tool is a governed
change (docs/DECISIONS.md). Every tool reads trades only through the semantic layer
(gridcase/semantic/kpis.py) at its own as_of."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from gridcase.explore.query import ExploreQuery
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

Window = tuple[datetime, datetime]


class TotalsInputs(BaseModel):
    kpis: list[str]
    current: Window
    previous: Window | None = None


class ShareShiftInputs(BaseModel):
    dimension: str = "category"
    current: Window
    previous: Window | None = None


class MoversInputs(BaseModel):
    window: Window  # one day
    baseline_days: int = Field(default=7, ge=1, le=28)
    top_n: int = Field(default=5, ge=1, le=20)


class SwingsInputs(BaseModel):
    window: Window
    top_n: int = Field(default=5, ge=1, le=20)


class ByDayInputs(BaseModel):
    kpi: str
    window: Window


class PeriodInputs(BaseModel):
    window: Window


# the daily digest's checks: run by the digest only, not offered to the planner or the model
DIGEST_INPUTS: dict[str, type[BaseModel]] = {
    "digest.totals": TotalsInputs,
    "digest.share_shift": ShareShiftInputs,
    "digest.movers": MoversInputs,
    "digest.odds_swings": SwingsInputs,
    "digest.by_day": ByDayInputs,
    "digest.retention": PeriodInputs,
    "digest.rhythm": PeriodInputs,
    "digest.concentration": PeriodInputs,
}

# the Explore chat's query: run by the chat only, not offered to the planner
EXPLORE_INPUTS: dict[str, type[BaseModel]] = {"kpi.series": ExploreQuery}

ALL_INPUTS: dict[str, type[BaseModel]] = {**TOOL_INPUTS, **DIGEST_INPUTS, **EXPLORE_INPUTS}
