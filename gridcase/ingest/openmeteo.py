"""Open-Meteo source: hourly temperature, wind and cloud cover at each weather zone.

Backfill uses the ERA5 archive endpoint (one call per zone per year); live mode uses the
forecast endpoint with past_days so the last few days are re-observed. No key required.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from gridcase.ingest.base import RawCache, RetryingClient, day_chunks
from gridcase.store.models import Entity, RawObs
from gridcase.store.seed import load_entities

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
VARS = {
    "temperature_2m": "om.temp_2m",
    "wind_speed_10m": "om.wind_10m",
    "cloud_cover": "om.cloud_cover",
}
ARCHIVE_LAG_DAYS = 7  # ERA5 final data trails real time by several days


class OpenMeteoSource:
    name = "openmeteo"

    def __init__(
        self,
        client: httpx.Client,
        raw_dir: Path | None,
        zones: list[Entity] | None = None,
        live: bool = False,
        retry_sleep: float = 1.0,
    ) -> None:
        self.http = RetryingClient(client, min_interval_s=0.2, retry_sleep=retry_sleep)
        self.cache = RawCache(raw_dir, "openmeteo")
        self.zones = zones or [e for e in load_entities() if e.kind == "weather_zone"]
        self.live = live

    def chunks(self, start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
        return day_chunks(start, end, 366)

    def fetch(self, start: datetime, end: datetime) -> list[RawObs]:
        out: list[RawObs] = []
        for z in self.zones:
            body = self._fetch_zone(z, start, end)
            out += self._parse(z.entity_id, body, start, end)
        return out

    def _fetch_zone(self, z: Entity, start: datetime, end: datetime) -> dict[str, Any]:
        assert z.lat is not None and z.lon is not None
        base = {"latitude": z.lat, "longitude": z.lon, "hourly": ",".join(VARS), "timezone": "UTC"}
        if self.live:
            params = {**base, "past_days": 3, "forecast_days": 1}
            body: dict[str, Any] = self.http.get_json(FORECAST, params)
            return body
        cache_path = self.cache.path("archive", z.entity_id, start, end)
        cached = self.cache.get(cache_path)
        if cached is not None:
            return dict(cached)
        params = {
            **base,
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": (end - timedelta(hours=1)).strftime("%Y-%m-%d"),
        }
        body = self.http.get_json(ARCHIVE, params)
        # Only cache windows that are safely final.
        if end < datetime.utcnow() - timedelta(days=ARCHIVE_LAG_DAYS):
            self.cache.put(cache_path, body)
        return body

    @staticmethod
    def _parse(
        entity_id: str, body: dict[str, Any], start: datetime, end: datetime
    ) -> list[RawObs]:
        hourly = body.get("hourly", {})
        times = [datetime.strptime(t, "%Y-%m-%dT%H:%M") for t in hourly.get("time", [])]
        out: list[RawObs] = []
        for var, series_id in VARS.items():
            for t, v in zip(times, hourly.get(var, []), strict=False):
                if v is None or not (start <= t < end):
                    continue
                out.append(
                    RawObs(series_id=series_id, entity_id=entity_id, valid_time=t, value=float(v))
                )
        return out
