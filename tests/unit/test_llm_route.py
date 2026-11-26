"""GRIDCASE_LLM_ROUTE picks where the Claude calls go and which key counts as present."""

from __future__ import annotations

import pytest

from gridcase import config
from gridcase.planner.llm import claude_client, price_of


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    return monkeypatch


def test_default_route_is_anthropic(keys: pytest.MonkeyPatch) -> None:
    keys.delenv("GRIDCASE_LLM_ROUTE", raising=False)
    assert config.llm_route() == "anthropic"
    assert not config.has_claude_key()  # only the OpenRouter key is set
    keys.setenv("ANTHROPIC_API_KEY", "a-test")
    client, model = claude_client("claude-opus-5")
    assert model == "claude-opus-5"
    assert "openrouter" not in str(client.base_url)


def test_openrouter_route(keys: pytest.MonkeyPatch) -> None:
    keys.setenv("GRIDCASE_LLM_ROUTE", "openrouter")
    assert config.llm_route() == "openrouter"
    assert config.has_claude_key()
    client, model = claude_client("claude-opus-5")
    assert model == "anthropic/claude-opus-5"
    assert str(client.base_url).startswith("https://openrouter.ai/api")
    assert client.auth_token == "or-test"
    assert claude_client("anthropic/claude-sonnet-5")[1] == "anthropic/claude-sonnet-5"


def test_unknown_route_falls_back(keys: pytest.MonkeyPatch) -> None:
    keys.setenv("GRIDCASE_LLM_ROUTE", "bogus")
    assert config.llm_route() == "anthropic"


def test_price_ignores_prefix() -> None:
    assert price_of("anthropic/claude-opus-5") == price_of("claude-opus-5")
