"""The hourly watch: when the KPI alarm fires for a region, open an investigation
automatically, once per event, and cap how many use the paid model per day."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from gridcase.bench.inject import build_scratch
from gridcase.memory.cases import list_cases
from gridcase.planner.llm import FakeLLM
from gridcase.store.db import connect
from gridcase.watch import watch
from tests.conftest import FIXTURE_PARQUET

URI = datetime(2021, 2, 16, 12)


class Counting:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> FakeLLM:
        self.n += 1
        return FakeLLM()


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "w.duckdb"
    build_scratch(FIXTURE_PARQUET, path)
    return path


def test_an_alarm_opens_an_investigation_that_records_why(store: Path) -> None:
    conn = connect(store)
    paid, free = Counting(), Counting()
    opened = watch(conn, URI, paid, free, max_paid_per_day=5, entities=["ERCO"])
    assert len(opened) == 1
    case = next(c for c in list_cases(conn) if c.case_id == opened[0])
    assert case.entity == "ERCO" and case.as_of == URI and case.conclusion is not None
    trigger = [r for r in case.receipts if r.tool == "kpi.alarm"]
    assert len(trigger) == 1 and trigger[0].output["alarm"] is True
    assert paid.n == 1 and free.n == 0
    conn.close()


def test_the_same_event_is_not_investigated_twice(store: Path) -> None:
    conn = connect(store)
    first = watch(conn, URI, FakeLLM, FakeLLM, max_paid_per_day=5, entities=["ERCO"])
    again = watch(
        conn, datetime(2021, 2, 16, 13), FakeLLM, FakeLLM, max_paid_per_day=5, entities=["ERCO"]
    )
    assert len(first) == 1 and again == []
    conn.close()


def test_no_alarm_no_investigation(store: Path) -> None:
    conn = connect(store)
    assert (
        watch(
            conn, datetime(2023, 5, 3, 12), FakeLLM, FakeLLM, max_paid_per_day=5, entities=["ERCO"]
        )
        == []
    )
    assert list_cases(conn) == []
    conn.close()


def test_past_the_daily_cap_investigations_use_the_free_playbook(store: Path) -> None:
    conn = connect(store)
    paid, free = Counting(), Counting()
    opened = watch(conn, URI, paid, free, max_paid_per_day=0, entities=["ERCO"])
    assert len(opened) == 1 and paid.n == 0 and free.n == 1
    conn.close()


def test_a_region_without_data_is_skipped_quietly(store: Path) -> None:
    conn = connect(store)
    assert watch(conn, URI, FakeLLM, FakeLLM, max_paid_per_day=5, entities=["NOPE"]) == []
    conn.close()
