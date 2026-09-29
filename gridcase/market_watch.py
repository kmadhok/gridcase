"""The MarketCase hourly watch: check every watched series and open one case per event.

A case's `entity` is the segment ('all', 'category=Sports') and its `metric` the KPI. The alarm
receipt is the case's first evidence. Up to `max_paid_per_day` automatic investigations a UTC
day use the paid model, then the free standard playbook (M4 wires the investigation)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import duckdb

from gridcase.ledger.store import write_receipt
from gridcase.market_alarm import WatchedSeries, check_series, load_watch
from gridcase.memory.cases import list_cases, new_case
from gridcase.planner.llm import LLM

log = logging.getLogger("gridcase")


def watch_markets(
    conn: duckdb.DuckDBPyConnection,
    now: datetime,
    paid_factory: Callable[[], LLM],
    free_factory: Callable[[], LLM],
    max_paid_per_day: int,
    watched: list[WatchedSeries] | None = None,
    dedupe_hours: int = 24,
) -> list[str]:
    series_list = watched if watched is not None else load_watch()[0]
    opened: list[str] = []
    verdicts: dict[str, str] = {}
    for series in series_list:
        try:
            receipt = check_series(conn, series, now)
        except Exception as e:  # noqa: BLE001 - one series must not stop the rest
            log.warning("alarm check failed for %s: %s", series.label, e)
            verdicts[series.label] = "error"
            continue
        o = receipt.output
        verdicts[series.label] = o["status"] if o["status"] != "alarm" else f"ALARM x{o['ratio']}"
        if o["status"] != "alarm":
            continue
        if any(
            c.entity == series.segment
            and c.metric == series.kpi
            and now - timedelta(hours=dedupe_hours) <= c.as_of <= now
            for c in list_cases(conn)
        ):
            continue
        as_of = now.replace(second=0, microsecond=0)
        case = new_case(conn, entity=series.segment, metric=series.kpi, as_of=as_of)
        write_receipt(conn, case.case_id, receipt)
        opened.append(case.case_id)
    log.info("market watch %s: %s", now.isoformat(timespec="minutes"), verdicts)
    return opened
