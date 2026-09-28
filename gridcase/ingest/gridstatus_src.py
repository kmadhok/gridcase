"""gridstatus source: day-ahead and real-time hourly hub LMP for ERCOT, CAISO, PJM.

gridstatus is an optional dependency (uv sync --extra gridstatus) pinned in pyproject.
Scrapers break and history is patchy, so every ISO call fails soft: a failure is logged,
that ISO contributes no rows, and the run is marked 'partial' rather than 'failed'.
Coverage per ISO/period must be verified before any hypothesis relies on it (Phase 2 gate).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from gridcase.ingest.base import RawCache, day_chunks
from gridcase.store.models import RawObs

log = logging.getLogger("gridcase")

# hub entity -> (iso class name, gridstatus location id, market for DA, market for RT hourly)
HUBS: dict[str, tuple[str, str]] = {
    "ERCO/HB_HUBAVG": ("Ercot", "HB_HUBAVG"),
    "CISO/TH_NP15_GEN-APND": ("CAISO", "TH_NP15_GEN-APND"),
    "CISO/TH_SP15_GEN-APND": ("CAISO", "TH_SP15_GEN-APND"),
    "PJM/WESTERN_HUB": ("PJM", "WESTERN HUB"),
}


class GridstatusSource:
    name = "gridstatus"

    def __init__(self, raw_dir: Path | None, hubs: list[str] | None = None) -> None:
        self.cache = RawCache(raw_dir, "gridstatus")
        self.hubs = hubs or list(HUBS)
        self.partial_errors: list[str] = []

    def chunks(self, start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
        return day_chunks(start, end, 1)

    def fetch(self, start: datetime, end: datetime) -> list[RawObs]:
        try:
            import gridstatus
        except ImportError:
            self.partial_errors.append("gridstatus not installed (uv sync --extra gridstatus)")
            log.warning("gridstatus not installed; skipping")
            return []
        out: list[RawObs] = []
        for hub in self.hubs:
            iso_name, location = HUBS[hub]
            # v1 pulls day-ahead hourly only. REAL_TIME_HOURLY is not offered by gridstatus
            # for these ISOs; RT would need 5/15-min pulls aggregated here (deferred).
            for market, series_id in (("DAY_AHEAD_HOURLY", "gs.lmp_da_hub"),):
                try:
                    out += self._fetch_hub(
                        gridstatus, iso_name, location, hub, market, series_id, start, end
                    )
                except Exception as e:  # scrapers raise anything; fail soft by design
                    msg = f"{hub} {market} {start:%Y-%m-%d}: {type(e).__name__}: {str(e)[:120]}"
                    self.partial_errors.append(msg)
                    log.warning("gridstatus %s", msg)
        return out

    def _fetch_hub(
        self,
        gridstatus: Any,
        iso_name: str,
        location: str,
        hub: str,
        market: str,
        series_id: str,
        start: datetime,
        end: datetime,
    ) -> list[RawObs]:
        cache_path = self.cache.path(f"{iso_name}_{market}", hub, start, end)
        cached = self.cache.get(cache_path)
        if cached is not None:
            df = pd.DataFrame(cached)
        else:
            iso = getattr(gridstatus, iso_name)()
            if iso_name == "Ercot":
                df = iso.get_spp(
                    date=start,
                    end=end,
                    market=market,
                    locations=[location],
                    location_type="Trading Hub",
                )
            else:
                df = iso.get_lmp(date=start, end=end, market=market, locations=[location])
            df = df[["Interval Start", "Location", "LMP" if "LMP" in df.columns else "SPP"]].copy()
            df.columns = ["t", "loc", "price"]
            df["t"] = pd.to_datetime(df["t"], utc=True).dt.strftime("%Y-%m-%dT%H:%M")
            self.cache.put(cache_path, df.to_dict(orient="records"))
        out: list[RawObs] = []
        for rec in df.to_dict(orient="records"):
            t = datetime.strptime(str(rec["t"]), "%Y-%m-%dT%H:%M")
            if start <= t < end and rec["price"] is not None:
                out.append(
                    RawObs(
                        series_id=series_id, entity_id=hub, valid_time=t, value=float(rec["price"])
                    )
                )
        return out
