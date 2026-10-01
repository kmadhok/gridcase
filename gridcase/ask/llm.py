"""Askers: a deterministic FakeAsker (tests, and the demo without an API key) and the
Anthropic tool loop. Checks are strict tools whose input shapes are the planner's typed ones
(`llm_schema.LLM_INPUTS`); the answer is a forced `submit_answer` tool call."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from gridcase import config
from gridcase.ask.context import cite_code
from gridcase.ask.engine import MAX_CHECKS, RunCheck
from gridcase.ask.glossary import CHECKS
from gridcase.ask.schema import Answer
from gridcase.explain.schema import Claim
from gridcase.ledger.schema import Receipt
from gridcase.planner.llm import LLMUsage
from gridcase.planner.llm_schema import LLM_INPUTS
from gridcase.tools.base import ToolError

log = logging.getLogger("gridcase")

ASK_SYSTEM = f"""You answer a visitor's question about one page of GridCase: either one
investigation into why a US grid region's electricity demand differed from its day-ahead
forecast, or the scorecard of how accurate those investigations are.

Use ONLY the context you are given (causes tested and what rules each out, the plan, the
evidence with receipt ids and output numbers, the follow-up check, the glossary) and any
checks you run. Do not use outside knowledge about the grid or the event as fact.

Write for a smart reader who is not a grid engineer: short plain sentences, no statistics
jargon (say "30 times its normal variation", not "|z| = 30"), no internal ids (say "supply
shortfall", not H2_SUPPLY_SHORTFALL). Never write field or variable names from the evidence
(sign_consistent, max_abs_z, share_explained, flags that "came back as 0"): say what they
mean ("demand moved the opposite way to what the cold would cause"). Never mention receipt
ids or tool names in the text.

Every piece of evidence has a short code (cite_as). Every sentence that states a fact must
list the codes of the evidence it uses in receipt_ids (e.g. ["3f9a1c2e"]); when you restate a
conclusion sentence, reuse its cites. Every sentence that states a fact must cite,
and every number in it must be copied from that evidence (rounding is fine). A sentence that
contains any number but cites nothing is deleted before the reader sees it, so always cite.
Do not add facts from outside the context (no "during the freeze", no event names or causes
the evidence does not state). Tier: governed =
a number read directly from one piece of evidence; derived = a comparison or calculation
across evidence; inferred = an explanation of a term or a judgement (no citation needed).

If the evidence does not answer the question, say so plainly rather than guessing. When there
is no usual level yet (our record is young), still answer what the current numbers show (the
split by category or market, the largest segments) and say that comparisons need more days. On an
investigation page you may run up to {MAX_CHECKS} read-only checks (the check_* tools). Run one
whenever the question asks about a specific day, hour, region or quantity that the evidence on
hand only covers in aggregate or not at all (e.g. "the day before", "at 6 pm", "in PJM"):
never estimate a number you could check. Checks see the data only as it was known at the
investigation's point in time, so they cannot reveal what happened later; say so if asked
about later events. If the question is not about this page (sports, other topics, requests to
do something), call submit_answer with refused=true and no sentences. Always finish by calling
submit_answer; keep answers to at most five sentences."""


def _all_required(schema: Any) -> Any:
    """Every property required, in every nested object. The API allows at most 24 optional
    parameters across a request's strict tools; the model fills in each field (an empty list,
    the usual 7 days) and Python keeps its defaults for callers that leave them out."""
    if isinstance(schema, list):
        return [_all_required(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _all_required(v) for k, v in schema.items()}
    if isinstance(out.get("properties"), dict):
        out["required"] = list(out["properties"])
    return out


def _strict(model: Any) -> dict[str, Any]:
    from anthropic import transform_schema

    schema: dict[str, Any] = _all_required(transform_schema(model.model_json_schema()))
    return schema


def check_name(tool: str) -> str:
    return "check_" + tool.replace(".", "_")


def check_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": check_name(tool),
            "description": CHECKS.get(tool, tool)
            + " Read-only; runs on the data as known at the investigation's point in time.",
            "input_schema": _strict(model),
            "strict": True,
        }
        for tool, model in sorted(LLM_INPUTS.items())
    ]


ANSWER_TOOL_NAME = "submit_answer"


def answer_tool() -> dict[str, Any]:
    return {
        "name": ANSWER_TOOL_NAME,
        "description": "Submit the final answer: plain sentences, each citing its evidence.",
        "input_schema": _strict(Answer),
        "strict": True,
    }


def _no_llm_answer(_: list[Receipt]) -> Answer:
    return Answer(
        sentences=[
            Claim(
                text=(
                    "This copy of GridCase is running without a language model, so it cannot "
                    "answer free-form questions; the evidence behind every sentence on the page "
                    "is under 'show evidence'."
                ),
                tier="inferred",
            )
        ]
    )


class FakeAsker:
    """Runs a scripted list of checks, then builds the answer from what they returned."""

    def __init__(
        self,
        checks: list[tuple[str, dict[str, Any]]] | None = None,
        answer: Callable[[list[Receipt]], Answer] | None = None,
    ) -> None:
        self.checks = checks or []
        self.answer_fn = answer or _no_llm_answer
        self.calls = 0
        self.checks_attempted = 0
        self.check_errors: list[str] = []

    def ask(
        self, question: str, context: dict[str, Any], run_check: RunCheck, checks_enabled: bool
    ) -> tuple[Answer | None, LLMUsage]:
        self.calls += 1
        ran: list[Receipt] = []
        for tool, raw in self.checks:
            self.checks_attempted += 1
            try:
                ran.append(run_check(tool, raw))
            except ToolError as e:
                self.check_errors.append(str(e))
        return self.answer_fn(ran), LLMUsage()


class AnthropicAsker:
    max_checks = MAX_CHECKS

    def __init__(self, model: str | None = None) -> None:
        import anthropic

        self.model = model or config.ASK_MODEL
        self.client = anthropic.Anthropic()
        self.calls = 0

    def _usage(self, response: Any) -> LLMUsage:
        inp, out = int(response.usage.input_tokens), int(response.usage.output_tokens)
        pi, po = config.MODEL_PRICES.get(self.model, (5.0, 25.0))
        return LLMUsage(
            input_tokens=inp,
            output_tokens=out,
            cost_usd=(inp * pi + out * po) / 1e6,
            model=self.model,
        )

    def ask(
        self, question: str, context: dict[str, Any], run_check: RunCheck, checks_enabled: bool
    ) -> tuple[Answer | None, LLMUsage]:
        import anthropic

        by_name = {check_name(t): t for t in LLM_INPUTS}
        answer = answer_tool()
        tools = [*check_tools(), answer] if checks_enabled else [answer]
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": json.dumps({"context": context, "question": question}, default=str),
            }
        ]
        usage = LLMUsage(model=self.model)
        checks = 0
        for turn in range(self.max_checks + 2):
            final = not checks_enabled or checks >= self.max_checks or turn == self.max_checks + 1
            try:
                resp = self.client.messages.create(  # type: ignore[call-overload]
                    model=self.model,
                    max_tokens=4000,
                    system=ASK_SYSTEM,
                    messages=messages,
                    tools=[answer] if final else tools,
                    tool_choice={"type": "tool", "name": ANSWER_TOOL_NAME}
                    if final
                    else {"type": "any"},
                )
            except anthropic.APIError as e:
                log.error("ask call failed: %s", e)
                return None, usage
            self.calls += 1
            usage = usage + self._usage(resp)
            messages.append({"role": "assistant", "content": resp.content})
            results: list[dict[str, Any]] = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                if block.name == ANSWER_TOOL_NAME:
                    try:
                        return Answer.model_validate(block.input), usage
                    except ValidationError:
                        return None, usage
                try:
                    rec = run_check(by_name[block.name], dict(block.input))
                    checks += 1
                    content = json.dumps(
                        {"cite_as": cite_code(rec.receipt_id), "output": rec.output}, default=str
                    )
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": content}
                    )
                except (ToolError, KeyError) as e:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(e),
                            "is_error": True,
                        }
                    )
            if not results:
                return None, usage
            messages.append({"role": "user", "content": results})
        return None, usage


def default_asker_factory() -> Callable[[], Any]:
    return AnthropicAsker if config.has_anthropic_api_key() else FakeAsker
