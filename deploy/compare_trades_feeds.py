"""Compare one collected hour of trades with what /v2/trades serves for the same hour.

Read-only: reads the collector's raw file for the hour and walks /v2/trades back to its start;
writes nothing and does not touch the collector or the store. Run it on the box whose
collector saved the hour (with v1, before the switch), once the hour has closed:

    uv run python deploy/compare_trades_feeds.py 2026-10-16T09

Prints the trade count in the raw file, in v2, in both (same trade_key), and in only one.
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx

from gridcase import config
from gridcase.ingest.polymarket import BASE_URL, _hour_file, trade_key, v2_since


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("hour", help="UTC hour, e.g. 2026-10-16T09")
    ap.add_argument("--max-pages", type=int, default=400, help="v2 pages to walk at most")
    args = ap.parse_args()
    start = datetime.strptime(args.hour, "%Y-%m-%dT%H").replace(tzinfo=UTC)
    lo, hi = int(start.timestamp()), int((start + timedelta(hours=1)).timestamp())
    path = _hour_file(config.RAW_DIR / "polymarket", lo)
    with gzip.open(path, "rt") as f:
        raw = {trade_key(t) for t in map(json.loads, f) if lo <= int(t["timestamp"]) < hi}
    with httpx.Client(base_url=BASE_URL, headers={"User-Agent": "gridcase"}) as client:
        rows, complete = v2_since(client, lo, args.max_pages)
    v2 = {trade_key(t) for t in rows if int(t["timestamp"]) < hi}
    print(f"hour {args.hour}Z  file {path}")
    print(f"raw file (collected) {len(raw):>8,}")
    print(f"v2 /v2/trades        {len(v2):>8,}{'' if complete else '  (page cap hit: partial)'}")
    print(f"in both              {len(raw & v2):>8,}")
    print(f"only in raw file     {len(raw - v2):>8,}")
    print(f"only in v2           {len(v2 - raw):>8,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
