"""`gridcase` command line. Phase 1: init and ingest. Later phases add replay/bench/serve."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

import httpx

from gridcase import config
from gridcase.ingest.base import Source
from gridcase.ingest.runner import Mode, run_ingest
from gridcase.store.db import connect, one

SOURCES = ["eia", "openmeteo", "gridstatus"]


def _parse_date(s: str) -> datetime:
    return (
        datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        if "T" in s
        else datetime.fromisoformat(s)
    )


def live_window(now: datetime) -> tuple[datetime, datetime]:
    """Hourly mode: re-observe the last 3 days (revisions) and pull forecasts 2 days ahead."""
    top = now.replace(minute=0, second=0, microsecond=0)
    return top - timedelta(days=3), top + timedelta(days=2)


def resolve_window(start: str | None, end: str | None, now: datetime) -> tuple[datetime, datetime]:
    """Backfill window from CLI strings; defaults to the spec's history start and now."""
    s = _parse_date(start) if start else config.HISTORY_START
    e = _parse_date(end) if end else now
    if e <= s:
        raise SystemExit(f"--end {e} must be after --start {s}")
    return s, e


def build_source(name: str, live: bool) -> Source:
    client = httpx.Client(headers={"User-Agent": "gridcase"})
    if name == "eia":
        from gridcase.ingest.eia import EiaSource

        return EiaSource(client=client, api_key=config.eia_api_key(), raw_dir=config.RAW_DIR)
    if name == "openmeteo":
        from gridcase.ingest.openmeteo import OpenMeteoSource

        return OpenMeteoSource(client=client, raw_dir=config.RAW_DIR, live=live)
    if name == "gridstatus":
        from gridcase.ingest.gridstatus_src import GridstatusSource

        return GridstatusSource(raw_dir=config.RAW_DIR)
    raise SystemExit(f"unknown source {name}")


def cmd_ingest(args: argparse.Namespace) -> int:
    mode: Mode = "live" if args.live else "backfill"
    now = datetime.now(UTC).replace(tzinfo=None)
    start, end = live_window(now) if args.live else resolve_window(args.start, args.end, now)
    names = SOURCES if args.source == "all" else [args.source]
    conn = connect(config.DB_PATH)
    rc = 0
    for name in names:
        run_id = run_ingest(conn, build_source(name, args.live), start, end, mode)
        status, rows, err = one(
            conn, "SELECT status, rows_written, error FROM ingest_runs WHERE run_id = ?", [run_id]
        )
        print(f"{name}: {status}, {rows} new rows, run {run_id}" + (f" — {err}" if err else ""))
        rc = rc or (1 if status == "failed" else 0)
    conn.close()
    return rc


def cmd_init(_: argparse.Namespace) -> int:
    conn = connect(config.DB_PATH)
    n_series, n_ent = one(
        conn, "SELECT (SELECT count(*) FROM series), (SELECT count(*) FROM entities)"
    )
    print(f"{config.DB_PATH}: {n_series} series, {n_ent} entities")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="gridcase")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the store and seed series/entities").set_defaults(
        fn=cmd_init
    )
    ing = sub.add_parser("ingest", help="pull a source into the store")
    ing.add_argument("--source", choices=[*SOURCES, "all"], required=True)
    ing.add_argument("--start", help="YYYY-MM-DD[THH:MM] UTC; default 2018-07-01")
    ing.add_argument("--end", help="YYYY-MM-DD[THH:MM] UTC exclusive; default now")
    ing.add_argument(
        "--live", action="store_true", help="hourly mode: last 3 days to +2 days, real vintages"
    )
    ing.set_defaults(fn=cmd_ingest)
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
