"""The MarketCase hourly watch: check every watched series and open one case per event.

A case's `entity` is the segment ('all', 'category=Sports') and its `metric` the KPI. The alarm
receipt is the case's first evidence. Up to `max_paid_per_day` automatic investigations a UTC
day use the paid model, then the free standard playbook."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import duckdb

from gridcase.investigate import investigate
from gridcase.market_alarm import WatchedSeries, check_series, load_watch
from gridcase.memory.cases import list_cases
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
        today = now.date()
        paid_today = sum(
            1 for c in list_cases(conn) if c.opened_at.date() == today and c.llm_usage.cost_usd > 0
        )
        factory = paid_factory if paid_today < max_paid_per_day else free_factory
        as_of = now.replace(second=0, microsecond=0)
        try:
            case = investigate(
                conn, factory(), series.kpi, as_of, filters=series.filters, trigger=receipt
            )
        except Exception as e:  # noqa: BLE001 - one failed investigation must not stop the rest
            log.warning("investigation failed for %s: %s", series.label, e)
            continue
        log.info("alarm on %s: opened %s (%s)", series.label, case.case_id, case.top_hypothesis)
        opened.append(case.case_id)
    log.info("market watch %s: %s", now.isoformat(timespec="minutes"), verdicts)
    return opened
