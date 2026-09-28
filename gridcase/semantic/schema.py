"""Typed objects for the semantic layer."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from gridcase.semantic.expr import identifiers, parse_expr

Grain = Literal["hourly", "daily"]
ALL_GRAINS: tuple[Grain, ...] = ("hourly", "daily")
Dim = Literal["fuel", "subregion"]
Kind = Literal["series", "aggregate", "derived", "rolling_ratio"]


class MetricDef(BaseModel):
    id: str
    description: str
    unit: str
    kind: Kind
    authority: Literal["governed"] = "governed"
    entity_kind: Literal["ba", "hub"] = "ba"
    series: str | None = None
    expr: str | None = None
    agg: Literal["mean", "sum"] | None = None
    dims: list[Dim] = Field(default_factory=list)
    grains: list[Grain] = Field(default_factory=lambda: list(ALL_GRAINS))
    daily_agg: Literal["mean", "sum"] = "mean"
    numerator: str | None = None
    numerator_dims: dict[str, str] = Field(default_factory=dict)
    trailing_hours: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> MetricDef:
        if self.kind in ("series", "aggregate") and not self.series:
            raise ValueError(f"{self.id}: kind {self.kind} needs `series`")
        if self.kind == "aggregate" and not self.agg:
            raise ValueError(f"{self.id}: aggregate needs `agg`")
        if self.kind == "derived" and not self.expr:
            raise ValueError(f"{self.id}: derived needs `expr`")
        if self.kind == "rolling_ratio" and not (self.numerator and self.trailing_hours):
            raise ValueError(f"{self.id}: rolling_ratio needs `numerator` and `trailing_hours`")
        if self.expr:
            parse_expr(self.expr)
        return self

    def dependencies(self) -> list[str]:
        if self.kind == "derived" and self.expr:
            return sorted(identifiers(parse_expr(self.expr)))
        if self.kind == "rolling_ratio" and self.numerator:
            return [self.numerator]
        return []


class MetricQuery(BaseModel):
    metric: str
    entity: str
    grain: str = "hourly"
    window: tuple[datetime, datetime]
    as_of: datetime
    dims: dict[str, str] = Field(default_factory=dict)


class CompiledQuery(BaseModel):
    sql: str
    columns: list[str]
    metrics_used: list[str]
    series_used: list[str]
