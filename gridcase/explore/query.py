"""The form the Explore chat fills in, like a Cube query: a KPI, an optional group-by
dimension, filters, a grain and a window. Every allowed value comes from ontology/kpis.yaml,
so the model can only pick from the semantic layer; the server compiles and runs the query
(`kpi.series`). Named windows are resolved here, at as_of, over complete hours only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from gridcase.semantic.kpis import is_internal, load_kpis

NamedWindow = Literal["last_24h", "last_3d", "last_7d", "today", "yesterday", "since_start"]
WindowName = NamedWindow | Literal["explicit"]
WINDOW_HELP: dict[str, str] = {
    "last_24h": "the last 24 complete hours",
    "last_3d": "the last 3 days (72 complete hours)",
    "last_7d": "the last 7 days; use for 'this week', 'past week', 'lately'",
    "today": "since 00:00 UTC today",
    "yesterday": "00:00 to 24:00 UTC yesterday",
    "since_start": "since our record began",
    "explicit": "start and end (UTC, end exclusive) when no named window fits",
}
_SPANS = {"last_24h": 1, "last_3d": 3, "last_7d": 7}
HOUR = timedelta(hours=1)


class QueryError(ValueError):
    pass


def _naive_utc(t: datetime | None) -> datetime | None:
    return t.astimezone(UTC).replace(tzinfo=None) if t is not None and t.tzinfo else t


Dt = Annotated[datetime | None, AfterValidator(_naive_utc)]


def filter_dimensions() -> list[str]:
    """Dimensions with a fixed list of segments. Market has none (thousands of free-text
    titles), so it may be grouped by but not filtered on."""
    _, dims = load_kpis()
    return [d for d, dim in dims.items() if dim.values]


def group_dimensions() -> list[str]:
    return [*filter_dimensions(), "market"]


def public_kpis() -> list[str]:
    kpis, _ = load_kpis()
    return [k for k, v in kpis.items() if not is_internal(v)]


class ExploreFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: str
    values: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _known(self) -> ExploreFilter:
        if self.dimension not in filter_dimensions():
            raise ValueError(f"cannot filter on {self.dimension!r}")
        allowed = load_kpis()[1][self.dimension].values or []
        unknown = [v for v in self.values if v not in allowed]
        if unknown:
            raise ValueError(f"{self.dimension} has no segment {unknown[0]!r}")
        return self


class ExploreQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kpi: str
    group_by: str | None = None
    filters: list[ExploreFilter] = Field(default_factory=list)
    grain: Literal["hour", "day", "week", "total"] = "total"
    window: WindowName
    start: Dt = None
    end: Dt = None
    top_n: int = Field(default=10, ge=1, le=20)

    @field_validator("kpi")
    @classmethod
    def _public_kpi(cls, v: str) -> str:
        if v not in public_kpis():
            raise ValueError(f"unknown KPI {v!r}")
        return v

    @field_validator("group_by")
    @classmethod
    def _groupable(cls, v: str | None) -> str | None:
        if v is not None and v not in group_dimensions():
            raise ValueError(f"cannot group by {v!r}")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> ExploreQuery:
        dims = [f.dimension for f in self.filters]
        if len(dims) != len(set(dims)):
            raise ValueError("one filter per dimension (list several values instead)")
        if self.window == "explicit":
            if self.start is None or self.end is None:
                raise ValueError("an explicit window needs start and end")
            if self.end <= self.start:
                raise ValueError("the window must end after it starts")
        elif self.start is not None or self.end is not None:
            raise ValueError(f"the named window {self.window!r} takes no start or end")
        return self

    def filter_map(self) -> dict[str, list[str]]:
        return {f.dimension: list(f.values) for f in self.filters}


def resolve_window(
    q: ExploreQuery, as_of: datetime, record_start: datetime | None
) -> tuple[datetime, datetime]:
    """The query's UTC window at as_of. Every window stops at the last complete hour, so a
    number never mixes finished hours with the one still running."""
    now = as_of.replace(minute=0, second=0, microsecond=0)
    midnight = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    if q.window in _SPANS:
        lo, hi = now - timedelta(days=_SPANS[q.window]), now
    elif q.window == "today":
        lo, hi = midnight, now
    elif q.window == "yesterday":
        lo, hi = midnight - timedelta(days=1), midnight
    elif q.window == "since_start":
        if record_start is None:
            raise QueryError("our record has no trades yet")
        lo, hi = record_start, now
    else:
        assert q.start is not None and q.end is not None  # the validator guarantees both
        lo, hi = q.start, min(q.end, now)
    if hi <= lo:
        raise QueryError("no complete hour in this window yet")
    return lo, hi


def form_schema() -> dict[str, Any]:
    """The JSON schema a model fills in (strict structured outputs). `anyOf` for the nullable
    group-by because Anthropic rejects an enum next to a type list."""
    _, dims = load_kpis()
    values = sorted({v for d in filter_dimensions() for v in dims[d].values or []})
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kpi", "group_by", "filters", "grain", "window", "start", "end"],
        "properties": {
            "kpi": {"type": "string", "enum": public_kpis()},
            "group_by": {
                "anyOf": [{"type": "string", "enum": group_dimensions()}, {"type": "null"}]
            },
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["dimension", "values"],
                    "properties": {
                        "dimension": {"type": "string", "enum": filter_dimensions()},
                        "values": {"type": "array", "items": {"type": "string", "enum": values}},
                    },
                },
            },
            "grain": {"type": "string", "enum": ["hour", "day", "week", "total"]},
            "window": {"type": "string", "enum": list(WINDOW_HELP)},
            "start": {"type": ["string", "null"]},
            "end": {"type": ["string", "null"]},
        },
    }
