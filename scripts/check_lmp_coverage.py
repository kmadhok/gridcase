"""Phase 2 gate item: does gridstatus return day-ahead hub LMP for each replay event?

Run on a machine with the optional extra installed:
    uv run --extra gridstatus python scripts/check_lmp_coverage.py
Prints one line per (event, hub) with the number of hourly rows obtained, or the error.
Nothing is written to the store; results belong in docs/DECISIONS.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from gridcase.ingest.gridstatus_src import GridstatusSource

EVENTS = {
    "uri_2021": ("ERCO", datetime(2021, 2, 14)),
    "caiso_2020": ("CISO", datetime(2020, 8, 15)),
    "elliott_2022": ("PJM", datetime(2022, 12, 24)),
    "tx_heat_2023": ("ERCO", datetime(2023, 6, 27)),
    "eclipse_2024_erco": ("ERCO", datetime(2024, 4, 8)),
    "eclipse_2024_ciso": ("CISO", datetime(2024, 4, 8)),
}


def main() -> None:
    logging.basicConfig(level=logging.ERROR)
    from gridcase.ingest.gridstatus_src import HUBS

    for name, (ba, day) in EVENTS.items():
        hubs = [h for h in HUBS if h.startswith(ba + "/")]
        src = GridstatusSource(raw_dir=None, hubs=hubs)
        rows = src.fetch(day, day + timedelta(days=1))
        per_hub = {h: sum(1 for r in rows if r.entity_id == h) for h in hubs}
        errs = "; ".join(src.partial_errors) or "-"
        print(f"{name:20s} {ba:5s} rows={per_hub} errors={errs[:160]}")


if __name__ == "__main__":
    main()
