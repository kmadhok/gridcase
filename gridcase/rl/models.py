"""M15 model adapters for the agent loop (gridcase/rl/agent.py).

AnthropicAgent  the frontier model (config.FRONTIER_MODEL) through the Anthropic SDK: a manual
                tool-use loop, because the episode runner owns the tool budget, the spend
                ledger and the verifier. No server-side fallbacks: a benchmark must not switch
                models mid-task. The stable tools+system prefix is cached.
TinkerAgent     the small open model (config.RL_BASE_MODEL, or a LoRA checkpoint by tinker://
                path) through Tinker's native SamplingClient and the Cookbook renderer, so the
                untuned and tuned rows use the same sampler, chat template and tool-call
                parser. Each turn keeps its prompt tokens, sampled tokens and logprobs for RL.

The Tinker SDK and Cookbook are an optional dependency group (`uv sync --group rl`); they are
imported only when a TinkerAgent is built.
"""

from __future__ import annotations

import json
from typing import Any

from gridcase import config
from gridcase.rl.agent import Session, ToolCall, Turn

# Tinker prices, USD per million tokens (prefill, sample, train): Models & Pricing,
# tinker-docs.thinkingmachines.ai, read 2026-10-12. Cached prefill is 80% cheaper; costs here
# assume no cache, so they are upper bounds.
TINKER_PRICES: dict[str, tuple[float, float, float]] = {"Qwen/Qwen3-8B": (0.195, 0.60, 0.44)}


class AnthropicAgent:
    def __init__(
        self,
        model: str | None = None,
        phase: str = "frontier_eval",
        estimate: float = 0.15,
        client: Any = None,
        max_tokens: int = 16000,
    ) -> None:
        self.name = model or config.FRONTIER_MODEL
        self.phase = phase
        self.estimate = estimate
        self.max_tokens = max_tokens
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client

    def session(self, system: str, tools: list[dict[str, Any]]) -> Session:
        return _AnthropicSession(self, system, tools)


class _AnthropicSession:
    def __init__(self, agent: AnthropicAgent, system: str, tools: list[dict[str, Any]]) -> None:
        self.agent = agent
        self.system = system
        self.tools = tools
        self.messages: list[dict[str, Any]] = []

    def send(self, user: str | None, results: list[tuple[str, str]]) -> Turn:
        content: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": tid, "content": text} for tid, text in results
        ]
        if user is not None:
            content.append({"type": "text", "text": user})
        self.messages.append({"role": "user", "content": content})
        resp = self.agent.client.messages.create(
            model=self.agent.name,
            max_tokens=self.agent.max_tokens,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            cache_control={"type": "ephemeral"},
        )
        # the whole content goes back, thinking blocks included (they are tied to the turn)
        self.messages.append({"role": "assistant", "content": resp.content})
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in resp.content
            if b.type == "tool_use"
        ]
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        fresh = int(getattr(u, "input_tokens", 0) or 0)
        written = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        out = int(getattr(u, "output_tokens", 0) or 0)
        pin, pout = config.MODEL_PRICES.get(self.agent.name, (5.0, 25.0))
        usd = (fresh * pin + written * pin * 1.25 + read * pin * 0.1 + out * pout) / 1e6
        return Turn(
            text=text,
            calls=calls,
            input_tokens=fresh + written + read,
            output_tokens=out,
            usd=usd,
            raw={"stop_reason": resp.stop_reason},
        )


class TinkerAgent:
    def __init__(
        self,
        base_model: str | None = None,
        model_path: str | None = None,
        phase: str = "small_eval",
        estimate: float = 0.02,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        renderer_name: str | None = None,
        sampling_client: Any = None,
    ) -> None:
        from tinker_cookbook.renderers import get_renderer
        from tinker_cookbook.tokenizer_utils import get_tokenizer

        self.base_model = base_model or config.RL_BASE_MODEL
        self.name = model_path or self.base_model
        self.phase = phase
        self.estimate = estimate
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.renderer = get_renderer(
            renderer_name or config.RL_RENDERER, get_tokenizer(self.base_model)
        )
        if sampling_client is None:
            import tinker

            service = tinker.ServiceClient()
            if model_path:
                sampling_client = service.create_sampling_client(model_path=model_path)
            else:
                sampling_client = service.create_sampling_client(base_model=self.base_model)
        self.sampler = sampling_client

    def session(self, system: str, tools: list[dict[str, Any]]) -> Session:
        return _TinkerSession(self, system, tools)


class _TinkerSession:
    def __init__(self, agent: TinkerAgent, system: str, tools: list[dict[str, Any]]) -> None:
        from tinker_cookbook.renderers import ToolSpec

        self.agent = agent
        specs = [
            ToolSpec(name=t["name"], description=t["description"], parameters=t["input_schema"])
            for t in tools
        ]
        self.messages: list[Any] = agent.renderer.create_conversation_prefix_with_tools(
            specs, system_prompt=system
        )
        self.names: dict[str, str] = {}
        self.turn = 0

    def send(self, user: str | None, results: list[tuple[str, str]]) -> Turn:
        from tinker import types

        r = self.agent.renderer
        for tid, text in results:
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tid,
                    "name": self.names.get(tid, ""),
                    "content": text,
                }
            )
        if user is not None:
            self.messages.append({"role": "user", "content": user})
        prompt = r.build_generation_prompt(self.messages)
        params = types.SamplingParams(
            max_tokens=self.agent.max_tokens,
            temperature=self.agent.temperature,
            stop=r.get_stop_sequences(),
        )
        res = self.agent.sampler.sample(
            prompt=prompt, num_samples=1, sampling_params=params
        ).result()
        seq = res.sequences[0]
        tokens = list(seq.tokens)
        msg, _ = r.parse_response(tokens)
        self.messages.append(msg)
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            tid = getattr(tc, "id", None) or f"call_{self.turn}_{i}"
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": tc.function.arguments}
            self.names[tid] = name
            calls.append(
                ToolCall(id=tid, name=name, arguments=args if isinstance(args, dict) else {})
            )
        self.turn += 1
        n_in = prompt.length
        prefill, sample, _ = TINKER_PRICES.get(self.agent.base_model, (0.3, 1.0, 1.0))
        content = msg.get("content")
        text = (
            content
            if isinstance(content, str)
            else "".join(str(c.get("text", "")) for c in content or [] if isinstance(c, dict))
        )
        return Turn(
            text=text,
            calls=calls,
            input_tokens=n_in,
            output_tokens=len(tokens),
            usd=(n_in * prefill + len(tokens) * sample) / 1e6,
            raw={
                "prompt_tokens": prompt.to_ints(),
                "tokens": tokens,
                "logprobs": list(seq.logprobs or []),
            },
        )
