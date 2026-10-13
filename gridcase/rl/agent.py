"""M15 agent loop: one episode = one task, the same for every model.

Every model gets the same system prompt, the same tools (the hist.* history tools plus
submit_answer), a budget of MAX_TOOL_CALLS tool calls and a cap on tool-output size. Tool
calls run through gridcase.tools.base.run_tool at the task's as_of on a read-only backfill
connection, so a model can only see what was known then. The final answer's summary and
claims pass gridcase/explain/verifier.py (hard rule 3): a number must appear in a receipt
the claim cites, and an uncited sentence that states a number is dropped.

Models implement AgentModel (gridcase/rl/models.py: Anthropic and Tinker); ScriptedAgent is
for tests and baselines. Paid calls reserve an upper-bound estimate in the spend ledger before
they run and record the actual cost after.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

import duckdb

from gridcase.explain.schema import Claim, Conclusion
from gridcase.explain.verifier import verify
from gridcase.ledger.schema import Receipt
from gridcase.rl.spend import OverBudget, Spend
from gridcase.rl.tasks import Task
from gridcase.tools.base import ToolError, run_tool
from gridcase.tools.registry import HIST_INPUTS

AGENT_TOOLS = [
    "hist.market",
    "hist.move",
    "hist.series",
    "hist.siblings",
    "hist.related",
    "hist.flow",
]
MAX_TOOL_CALLS = 8
MAX_TOOL_CHARS = 24_000  # ~6k tokens
MAX_TURNS = MAX_TOOL_CALLS + 3

DESCRIPTIONS = {
    "hist.market": "What the market asks: question, event, category, tags, outcomes, start and "
    "scheduled end. Prices are for the first outcome.",
    "hist.move": "The price now and its change over the last 1, 6, 24 and 72 hours, with the "
    "spread of hourly changes over the last 7 days.",
    "hist.series": "The price series up to now at a 10-minute, 1-hour or 1-day grain "
    "(at most 300 points).",
    "hist.siblings": "Other markets in the same event and how their prices moved over the "
    "last `hours`.",
    "hist.related": "Markets in other events that share a specific tag and moved at least "
    "`min_move_pts` points over the last `hours` (news hitting several markets).",
    "hist.flow": "Trading in this market over the last `hours` against the `hours` before: "
    "volume, trades, traders, buy share, whale share, volume by trade size.",
}
SUBMIT = {
    "name": "submit_answer",
    "description": "Give your final answer once. probability: your probability (0 to 1). "
    "summary: one or two sentences of reasoning without numbers. claims: factual sentences; "
    "every number in a claim must come from a tool result whose receipt_id the claim cites.",
    "input_schema": {
        "type": "object",
        "properties": {
            "probability": {"type": "number", "minimum": 0, "maximum": 1},
            "summary": {"type": "string"},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "receipt_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["text", "receipt_ids"],
                },
            },
        },
        "required": ["probability", "summary", "claims"],
    },
}
SYSTEM = (
    "You are an analyst investigating a Polymarket prediction market at a fixed moment in the "
    "past, called as_of. You can only learn about the market through the tools, which return "
    "governed evidence as known at as_of, each result with a receipt_id. Investigate with a few "
    "well-chosen tool calls (at most "
    f"{MAX_TOOL_CALLS}), then call submit_answer exactly once with your probability. Prices are "
    "probabilities of the market's first outcome. Do not guess facts you did not look up; every "
    "number you state in a claim must appear in a tool result you cite by receipt_id."
)


def api_name(tool: str) -> str:
    return tool.replace(".", "_")


TOOL_BY_API = {api_name(t): t for t in AGENT_TOOLS}


def tool_specs() -> list[dict[str, Any]]:
    """The tools every model sees, in a fixed order (JSON Schema from the Pydantic inputs)."""
    specs = []
    for t in AGENT_TOOLS:
        schema = HIST_INPUTS[t].model_json_schema()
        schema.pop("title", None)
        specs.append({"name": api_name(t), "description": DESCRIPTIONS[t], "input_schema": schema})
    return [*specs, SUBMIT]


def prompt_for(task: Task) -> str:
    f = task.facts
    when = task.as_of.strftime("%Y-%m-%d %H:%M UTC")
    head = f"Market condition_id: {task.condition_id}\nas_of: {when}\n\n"
    if task.kind == "spike_hold":
        later = (task.as_of + timedelta(hours=int(f["horizon_hours"]))).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        return head + (
            f"In the last {f['window_hours']} hours the market's price moved from "
            f"{f['price_before']} to {f['price_now']} ({f['move_pts']:+} points). "
            f"Question: what is the probability that at least half of this move is still there "
            f"{f['horizon_hours']} hours later, at {later}? Investigate why it moved, then "
            "submit your probability."
        )
    return head + (
        # no horizon: a forecaster knows the scheduled end (hist.market), not an early close
        f"The market's price now is {f['price_now']}. Question: what is the probability that "
        "the market's first outcome wins? Investigate, then submit your probability."
    )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    text: str
    calls: list[ToolCall]
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    raw: Any = None  # adapter-specific (e.g. sampled tokens and logprobs for training)


class Session(Protocol):
    def send(self, user: str | None, results: list[tuple[str, str]]) -> Turn: ...


class AgentModel(Protocol):
    name: str
    phase: str  # the spend ledger phase its calls count against
    estimate: float  # upper-bound cost of one turn, reserved before the call

    def session(self, system: str, tools: list[dict[str, Any]]) -> Session: ...


@dataclass
class Episode:
    task_id: str
    model: str
    probability: float | None = None
    summary: str = ""
    claims: list[Claim] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    tool_calls: int = 0
    turns: int = 0
    answer_turn: int | None = None  # index of the turn that called submit_answer
    budget_exceeded: bool = False
    malformed: bool = False
    error: str | None = None
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    raw_turns: list[Any] = field(default_factory=list)

    @property
    def penalised(self) -> bool:
        n = len(self.claims) + len(self.dropped)
        return self.budget_exceeded or (n > 0 and len(self.dropped) > n / 2)


def _content(receipt: Receipt) -> str:
    text = json.dumps({"receipt_id": receipt.receipt_id, "output": receipt.output}, default=str)
    return text if len(text) <= MAX_TOOL_CHARS else text[:MAX_TOOL_CHARS] + " …(truncated)"


def _finish(ep: Episode, args: dict[str, Any], receipts: dict[str, Receipt]) -> None:
    p = args.get("probability")
    if not isinstance(p, int | float) or isinstance(p, bool) or not 0.0 <= float(p) <= 1.0:
        ep.malformed = True
        return
    ep.probability = float(p)
    ep.summary = str(args.get("summary") or "")
    raw_claims = args.get("claims") or []
    claims = [Claim(text=ep.summary, receipt_ids=[])] if ep.summary else []
    for c in raw_claims if isinstance(raw_claims, list) else []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            ids = [str(r) for r in c.get("receipt_ids") or [] if isinstance(r, str)]
            claims.append(Claim(text=c["text"], receipt_ids=ids))
    concl = Conclusion(case_id=ep.task_id, top_hypothesis=None, ranked=[], claims=claims)
    kept, dropped = verify(concl, receipts, drop_uncited_numbers=True)
    ep.claims = [c for c in kept.claims if c.text != ep.summary or c.receipt_ids]
    ep.dropped = dropped


def run_episode(
    model: AgentModel, conn: duckdb.DuckDBPyConnection, task: Task, spend: Spend | None = None
) -> Episode:
    ep = Episode(task_id=task.task_id, model=model.name)
    t0 = time.perf_counter()
    sess = model.session(SYSTEM, tool_specs())
    user: str | None = prompt_for(task)
    ep.transcript.append({"role": "user", "content": user})
    results: list[tuple[str, str]] = []
    receipts: dict[str, Receipt] = {}
    nudged = False
    try:
        while ep.turns < MAX_TURNS:
            if spend is not None:
                spend.reserve(model.phase, model.estimate)
            turn = sess.send(user, results)
            user, results = None, []
            ep.turns += 1
            ep.usd += turn.usd
            ep.input_tokens += turn.input_tokens
            ep.output_tokens += turn.output_tokens
            ep.raw_turns.append(turn.raw)
            if spend is not None:
                spend.record(
                    model.phase, model.name, turn.input_tokens, turn.output_tokens, turn.usd
                )
            submit = next((c for c in turn.calls if c.name == "submit_answer"), None)
            if submit is not None:
                ep.answer_turn = ep.turns - 1
                _finish(ep, submit.arguments, receipts)
                break
            if not turn.calls:
                if nudged:
                    ep.malformed = True
                    break
                nudged = True
                user = "Call submit_answer now with your probability."
                ep.transcript.append({"role": "user", "content": user})
                continue
            step: dict[str, Any] = {"role": "tool", "calls": [], "results": []}
            for c in turn.calls:
                step["calls"].append((c.name, c.arguments))
                if ep.tool_calls >= MAX_TOOL_CALLS:
                    ep.budget_exceeded = True
                    content = json.dumps({"error": "tool budget used up; call submit_answer"})
                elif c.name not in TOOL_BY_API:
                    content = json.dumps({"error": f"unknown tool {c.name}"})
                else:
                    ep.tool_calls += 1
                    try:
                        r = run_tool(conn, TOOL_BY_API[c.name], c.arguments, task.as_of)
                        receipts[r.receipt_id] = r
                        content = _content(r)
                    except ToolError as e:
                        content = json.dumps({"error": str(e)})
                results.append((c.id, content))
                step["results"].append((c.id, content))
            ep.transcript.append(step)
        else:
            ep.malformed = ep.probability is None
        if ep.probability is None:
            ep.malformed = True
    except OverBudget as e:
        ep.error = str(e)
        ep.malformed = True
    except Exception as e:  # an API failure ends this episode, not the whole paid run
        ep.error = f"{type(e).__name__}: {e}"
        ep.malformed = True
    ep.seconds = time.perf_counter() - t0
    return ep


class ScriptedAgent:
    """A model whose turns come from a policy over the transcript (tests and baselines)."""

    def __init__(
        self,
        policy: Callable[[list[dict[str, Any]]], Turn],
        name: str = "scripted",
        phase: str = "reserve",
        estimate: float = 0.0,
    ) -> None:
        self.policy = policy
        self.name = name
        self.phase = phase
        self.estimate = estimate

    def session(self, system: str, tools: list[dict[str, Any]]) -> Session:
        return _ScriptedSession(self.policy)


class _ScriptedSession:
    def __init__(self, policy: Callable[[list[dict[str, Any]]], Turn]) -> None:
        self.policy = policy
        self.history: list[dict[str, Any]] = []

    def send(self, user: str | None, results: list[tuple[str, str]]) -> Turn:
        if user is not None:
            self.history.append({"role": "user", "content": user})
        if results:
            self.history.append({"role": "tool", "results": results})
        return self.policy(list(self.history))
