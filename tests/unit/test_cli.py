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


def test_serve_defaults_bind_tailscale_port_and_hourly_ingest() -> None:
    from gridcase.cli import serve_options

    opts = serve_options([])
    assert opts.port == 8710
    assert opts.host == "127.0.0.1"
    assert opts.ingest_interval_s == 3600.0
    assert opts.drain_interval_s == 600.0


def test_serve_can_disable_ingest_for_a_read_only_server() -> None:
    from gridcase.cli import serve_options

    opts = serve_options(["--host", "100.115.94.44", "--no-ingest"])
    assert opts.host == "100.115.94.44"
    assert opts.ingest_interval_s is None


def test_serve_llm_defaults_to_auto_and_can_be_forced() -> None:
    from gridcase.cli import serve_options

    assert serve_options([]).llm == "auto"
    assert serve_options(["--llm", "fake"]).llm == "fake"


def test_serve_auto_uses_the_fake_llm_only_when_no_api_key_is_present() -> None:
    from gridcase.cli import choose_llm_factory
    from gridcase.planner.llm import AnthropicLLM, FakeLLM

    assert choose_llm_factory("auto", has_key=False) is FakeLLM
    assert choose_llm_factory("auto", has_key=True) is AnthropicLLM
    assert choose_llm_factory("fake", has_key=True) is FakeLLM
    assert choose_llm_factory("anthropic", has_key=False) is AnthropicLLM
