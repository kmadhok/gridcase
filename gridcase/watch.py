"""The hourly watch: check each region's KPI alarm and, when it fires, open an investigation
automatically, once per event. Up to `max_paid_per_day` of these use the paid model; after
that the free standard playbook runs, so the site keeps working without surprise bills."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import duckdb

from gridcase import config
from gridcase.alarm import alarm_params, check_kpi
from gridcase.investigate import investigate
from gridcase.memory.cases import list_cases
from gridcase.planner.llm import LLM

log = logging.getLogger("gridcase")
CASE_METRIC = "forecast_error_mw"


def watch(
    conn: duckdb.DuckDBPyConnection,
    now: datetime,
    paid_factory: Callable[[], LLM],
    free_factory: Callable[[], LLM],
    max_paid_per_day: int,
    entities: list[str] | None = None,
    dedupe_hours: int = 24,
) -> list[str]:
    as_of = now.replace(minute=0, second=0, microsecond=0)
    opened: list[str] = []
    for entity in entities or config.REGIONS:
        try:
            receipt = check_kpi(conn, entity, as_of, alarm_params(entity))
        except Exception as e:  # noqa: BLE001 - one region's failure must not stop the rest
            log.warning("alarm check failed for %s: %s", entity, e)
            continue
        if not receipt.output["alarm"]:
            continue
        cases = list_cases(conn)
        if any(
            c.entity == entity and as_of - timedelta(hours=dedupe_hours) <= c.as_of <= as_of
            for c in cases
        ):
            continue
        today = datetime.now(UTC).date()
        paid_today = sum(
            1 for c in cases if c.opened_at.date() == today and c.llm_usage.cost_usd > 0
        )
        factory = paid_factory if paid_today < max_paid_per_day else free_factory
        try:
            case = investigate(conn, factory(), entity, CASE_METRIC, as_of, trigger=receipt)
        except Exception as e:  # noqa: BLE001
            log.warning("automatic investigation failed for %s: %s", entity, e)
            continue
        log.info("alarm on %s: opened %s (%s)", entity, case.case_id, case.top_hypothesis)
        opened.append(case.case_id)
    return opened
