"""OpenRouter client for the Explore chat: a cheap open-weight model returns JSON against a
strict schema, and OpenRouter reports what each call actually cost (the daily cap is charged
with that, so no price table is needed). On a provider error the fallback model is tried once."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from gridcase import config
from gridcase.planner.llm import LLMUsage

log = logging.getLogger("gridcase")
URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass
class JsonReply:
    data: dict[str, Any] | None  # None: no reply, or not a JSON object
    usage: LLMUsage
    error: str | None = None


class ChatModel(Protocol):
    def json(self, system: str, user: str, schema: dict[str, Any], name: str) -> JsonReply: ...


def _parse(text: str) -> dict[str, Any] | None:
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class OpenRouterChat:
    def __init__(
        self,
        model: str | None = None,
        fallback: str | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.models = [
            m
            for m in dict.fromkeys(
                [model or config.CHAT_MODEL, fallback or config.CHAT_FALLBACK_MODEL]
            )
            if m
        ]
        self.client = client or httpx.Client(timeout=timeout_s)

    def json(self, system: str, user: str, schema: dict[str, Any], name: str) -> JsonReply:
        usage, error = LLMUsage(), "no model configured"
        for model in self.models:
            reply = self._call(model, system, user, schema, name)
            usage = usage + reply.usage
            if reply.error is None:
                return JsonReply(reply.data, usage, None)
            error = reply.error
            log.warning("explore chat: %s failed: %s", model, error)
        return JsonReply(None, usage, error)

    def _call(
        self, model: str, system: str, user: str, schema: dict[str, Any], name: str
    ) -> JsonReply:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            },
            "provider": {"require_parameters": True},  # only hosts that honour the schema
            "usage": {"include": True},
            "temperature": 0,
            "max_tokens": 1500,
        }
        headers = {
            "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
            "X-Title": "MarketCase",
        }
        try:
            r = self.client.post(URL, json=body, headers=headers)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return JsonReply(None, LLMUsage(model=model), f"{type(e).__name__}")
        if r.status_code != 200 or "choices" not in data:
            return JsonReply(None, LLMUsage(model=model), f"HTTP {r.status_code}")
        u = data.get("usage") or {}
        usage = LLMUsage(
            input_tokens=int(u.get("prompt_tokens") or 0),
            output_tokens=int(u.get("completion_tokens") or 0),
            cost_usd=float(u.get("cost") or 0.0),
            model=model,
        )
        parsed = _parse(data["choices"][0]["message"].get("content") or "")
        # an unreadable reply is the model's failure, not the provider's: the caller retries it
        return JsonReply(parsed, usage)


class NoModelChat:
    """Without an OpenRouter key every reply is missing, so the chat says it cannot answer."""

    def json(self, system: str, user: str, schema: dict[str, Any], name: str) -> JsonReply:
        return JsonReply(None, LLMUsage(), "no OpenRouter key")


def default_chat_factory() -> type[OpenRouterChat] | type[NoModelChat]:
    return OpenRouterChat if config.has_openrouter_api_key() else NoModelChat
