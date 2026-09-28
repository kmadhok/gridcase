"""CLI behaviour that is pure: window arithmetic and source selection."""

from __future__ import annotations

from datetime import datetime

import pytest

from gridcase.cli import SOURCES, live_window, resolve_window


def test_live_window_is_three_days_back_two_days_forward_on_the_hour() -> None:
    now = datetime(2026, 9, 28, 22, 37, 15)
    start, end = live_window(now)
    assert start == datetime(2026, 9, 25, 22, 0)
    assert end == datetime(2026, 9, 30, 22, 0)


def test_resolve_window_defaults_to_history_start_and_now() -> None:
    now = datetime(2026, 9, 28, 22, 37, 15)
    start, end = resolve_window(start=None, end=None, now=now)
    assert start == datetime(2018, 7, 1)
    assert end == now


def test_resolve_window_parses_dates_and_utc_datetimes() -> None:
    start, end = resolve_window(
        start="2021-02-13", end="2021-02-21T12:00Z", now=datetime(2026, 1, 1)
    )
    assert start == datetime(2021, 2, 13)
    assert end == datetime(2021, 2, 21, 12, 0)


def test_resolve_window_rejects_inverted_range() -> None:
    with pytest.raises(SystemExit):
        resolve_window(start="2021-02-21", end="2021-02-13", now=datetime(2026, 1, 1))


def test_sources_are_the_three_spec_sources() -> None:
    assert SOURCES == ["eia", "openmeteo", "gridstatus"]
