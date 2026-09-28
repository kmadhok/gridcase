"""EIA API v2 source: region-data (D/DF/NG/TI), fuel-type-data, interchange-data, sub-BA demand.

Periods are requested with frequency=hourly, which EIA serves in UTC as 'YYYY-MM-DDTHH'.
Values are MWh per hour, i.e. average MW. Raw bodies are cached before parsing.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from gridcase.config import REGIONS
from gridcase.ingest.base import RawCache, RetryingClient, day_chunks, row_hash
from gridcase.store.models import RawObs

BASE = "https://api.eia.gov/v2/electricity/rto"
PAGE = 5000
REGION_TYPES = ["D", "DF", "NG", "TI"]

# route -> (facet name for the region filter, series_id, entity builder, chunk days)
ROUTES: dict[str, tuple[str, str, int]] = {
    "region-data": ("respondent", "eia.{type}", 30),
    "fuel-type-data": ("respondent", "eia.fuel", 5),
    "interchange-data": ("fromba", "eia.interchange", 10),
    "region-sub-ba-data": ("parent", "eia.subba_demand", 6),
}


def _entity(route: str, row: dict[str, Any]) -> str:
    if route == "region-data":
        return str(row["respondent"])
    if route == "fuel-type-data":
        return f"{row['respondent']}/{row['fueltype']}"
    if route == "interchange-data":
        return f"{row['fromba']}->{row['toba']}"
    return f"{row['parent']}/{row['subba']}"


def _series(route: str, row: dict[str, Any]) -> str:
    return ROUTES[route][1].format(type=row.get("type", ""))


def parse_period(p: str) -> datetime:
    return datetime.strptime(p, "%Y-%m-%dT%H")


class EiaSource:
    name = "eia"

    def __init__(
        self,
        client: httpx.Client,
        api_key: str,
        raw_dir: Path | None,
        regions: list[str] | None = None,
        routes: list[str] | None = None,
        retry_sleep: float = 1.0,
    ) -> None:
        self.http = RetryingClient(client, retry_sleep=retry_sleep, secret=api_key)
        self.api_key = api_key
        self.cache = RawCache(raw_dir, "eia")
        self.regions = regions or REGIONS
        self.routes = routes or list(ROUTES)

    # Smallest chunk across the routes we serve; each route further sub-chunks itself.
    def chunks(self, start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
        days = min(ROUTES[r][2] for r in self.routes)
        return day_chunks(start, end, days)

    def fetch(self, start: datetime, end: datetime) -> list[RawObs]:
        out: list[RawObs] = []
        for route in self.routes:
            if route == "region-data":
                for typ in REGION_TYPES:
                    out += self._fetch_route(route, start, end, {"facets[type][]": typ}, key=typ)
            else:
                out += self._fetch_route(route, start, end, {}, key="all")
        return out

    def _fetch_route(
        self, route: str, start: datetime, end: datetime, extra: dict[str, Any], key: str
    ) -> list[RawObs]:
        facet, _, _ = ROUTES[route]
        params: dict[str, Any] = {
            "frequency": "hourly",
            "data[0]": "value",
            f"facets[{facet}][]": self.regions,
            "start": start.strftime("%Y-%m-%dT%H"),
            "end": (end - timedelta(hours=1)).strftime("%Y-%m-%dT%H"),  # EIA end is inclusive
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            **extra,
        }
        rows = self._all_pages(route, params, key, start, end)
        out: list[RawObs] = []
        for row in rows:
            if row.get("value") is None:
                continue
            out.append(
                RawObs(
                    series_id=_series(route, row),
                    entity_id=_entity(route, row),
                    valid_time=parse_period(row["period"]),
                    value=float(row["value"]),
                    raw_hash=row_hash(row),
                )
            )
        return out

    def _all_pages(
        self, route: str, params: dict[str, Any], key: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        cache_path = self.cache.path(route, key, start, end)
        cached = self.cache.get(cache_path)
        if cached is not None:
            return list(cached)
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            body = self.http.get_json(
                f"{BASE}/{route}/data/",
                {**params, "offset": offset, "length": PAGE, "api_key": self.api_key},
            )
            resp = body.get("response", {})
            data: list[dict[str, Any]] = resp.get("data", [])
            rows.extend(data)
            total = int(resp.get("total", len(rows)))
            offset += len(data)
            if not data or offset >= total:
                break
        self.cache.put(cache_path, rows)
        return rows
