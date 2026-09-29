"""CLI behaviour that is pure: window arithmetic and source selection."""

from __future__ import annotations


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


def test_logging_never_echoes_request_urls_that_carry_api_keys() -> None:
    """httpx logs the full request URL (EIA's api_key is a query param) at INFO; it must be off."""
    import logging

    from gridcase.cli import configure_logging

    configure_logging()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_serve_watches_for_alarms_by_default_and_can_be_told_not_to() -> None:
    from gridcase.cli import serve_options

    assert serve_options([]).watch is True
    assert serve_options(["--no-watch"]).watch is False


def test_serve_defaults_bind_localhost_port_8710_and_drain_followups() -> None:
    from gridcase.cli import serve_options

    opts = serve_options([])
    assert (opts.host, opts.port, opts.drain_interval_s) == ("127.0.0.1", 8710, 600.0)


def test_the_cli_offers_the_marketcase_commands() -> None:
    import contextlib
    import io

    from gridcase.cli import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
        main(["--help"])
    for cmd in ("collect", "refresh", "investigate", "serve"):
        assert cmd in out.getvalue()
    for gone in ("ingest", "replay", "bench"):
        assert f" {gone} " not in out.getvalue()
