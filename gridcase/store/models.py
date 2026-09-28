"""Pydantic models for store rows. Datetimes are naive UTC; aware inputs are converted."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, field_validator

Vintage = Literal["observed", "synthetic"]


def to_naive_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t
    return t.astimezone(UTC).replace(tzinfo=None)


class Observation(BaseModel):
    """One fact: value of series for entity at valid_time, as known from available_at.

    observed_at  - when this process actually received the value (audit truth).
    available_at - when the value is treated as known for as_of queries. Equal to
                   observed_at for live rows; valid_time + lag_hours for backfilled
                   history whose real vintage is unknowable (vintage='synthetic').
    """

    series_id: str
    entity_id: str
    valid_time: datetime
    value: float
    observed_at: datetime
    available_at: datetime
    source: str
    vintage: Vintage = "observed"
    raw_hash: str | None = None

    @field_validator("valid_time", "observed_at", "available_at")
    @classmethod
    def _naive(cls, v: datetime) -> datetime:
        return to_naive_utc(v)


class RawObs(BaseModel):
    """What a source returns before the runner stamps observed_at/available_at."""

    series_id: str
    entity_id: str
    valid_time: datetime
    value: float
    raw_hash: str | None = None

    @field_validator("valid_time")
    @classmethod
    def _naive(cls, v: datetime) -> datetime:
        return to_naive_utc(v)


class Series(BaseModel):
    series_id: str
    name: str
    unit: str
    source: str
    lag_hours: int
    history_start: datetime | None = None


class Entity(BaseModel):
    entity_id: str
    kind: Literal["ba", "subregion", "fuel", "interchange_path", "weather_zone", "hub"]
    name: str
    parent_id: str | None = None
    tz: str | None = None
    lat: float | None = None
    lon: float | None = None


class IngestRun(BaseModel):
    run_id: str
    source: str
    started_at: datetime
    finished_at: datetime | None = None
    rows_written: int = 0
    status: Literal["running", "ok", "partial", "failed"] = "running"
    error: str | None = None
