"""M15 agent loop: one episode = one task, the same for every model.

Every model gets the same system prompt, the same tools (the hist.* history tools plus
submit_answer), a budget of MAX_TOOL_CALLS tool calls and a cap on tool-output size. Tool
calls run through gridcase.tools.base.run_tool at the task's as_of on a read-only backfill
connection, so a model can only see what was known then. The final answer's summary and
claims pass gridcase/explain/verifier.py (hard rule 3): a number must appear in a receipt
the claim cites, and an uncited sentence that states a number is dropped. The task's own facts
are a receipt too (the "task.facts" tool), and the summary is reasoning, not a checked claim
(user decision D2, 2026-10-14). The benchmark matches numbers strictly (verify(strict=True)).

Framing (user decision D1, 2026-10-14; config.RL_FRAMING): under "bd" a spike that fell is shown
as a rise of the market's second outcome, in the prompt and in the own-market tools (hist.market,
hist.move, hist.series), and the prompt states the market-implied hold probability. Other
markets' prices stay as they are, labelled. "a" is the original wording.

Models implement AgentModel (gridcase/rl/models.py: Anthropic and Tinker); ScriptedAgent is
for tests and baselines. Paid calls reserve an upper-bound estimate in the spend ledger before
they run and record the actual cost after.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Protocol

import duckdb

from gridcase import config
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
    "scheduled end. `price_is_for` names the outcome this market's prices are for.",
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
    "summary: one or two sentences of reasoning (not checked). claims: factual sentences; every "
    "number in a claim must come from a tool result or the task facts whose receipt_id the "
    "claim cites.",
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
    "probabilities of the market's first outcome unless the question or a tool result names "
    "another. Do not guess facts you did not look up; every number you state in a claim must "
    "appear in a tool result or the task facts you cite by receipt_id."
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


@dataclass(frozen=True)
class Frame:
    """How a task is shown. `flip`: a spike that fell, shown under "bd" as a rise of the market's
    second outcome (named `outcome` once hist.market has been read)."""

    framing: str = "a"
    flip: bool = False
    outcome: str = "the second outcome"


def frame_for(task: Task, framing: str) -> Frame:
    down = task.kind == "spike_hold" and float(task.facts["move_pts"]) < 0
    return Frame(framing=framing, flip=framing == "bd" and down)


def _shown(task: Task, frame: Frame) -> tuple[float, float, float]:
    """price_before, price_now and the move in points, in the outcome the task is shown in."""
    f = task.facts
    if frame.flip:
        return (
            round(1 - float(f["price_before"]), 4),
            round(1 - float(f["price_now"]), 4),
            -float(f["move_pts"]),
        )
    return f["price_before"], f["price_now"], f["move_pts"]


def hold_rate_implied(task: Task) -> float:
    """The market-implied chance the price ends on the side the move points to (harness.py's
    market baseline for spike_hold)."""
    p = float(task.facts["price_now"])
    return p if float(task.facts["move_pts"]) > 0 else 1.0 - p


def prompt_for(task: Task, framing: str = "a", facts_id: str | None = None) -> str:
    f = task.facts
    when = task.as_of.strftime("%Y-%m-%d %H:%M UTC")
    head = f"Market condition_id: {task.condition_id}\nas_of: {when}\n\n"
    cite = f" The numbers above are receipt {facts_id}." if facts_id else ""
    if task.kind == "spike_hold":
        frame = frame_for(task, framing)
        before, now, move = _shown(task, frame)
        whose = "the price of the market's second outcome" if frame.flip else "the market's price"
        later = (task.as_of + timedelta(hours=int(f["horizon_hours"]))).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        text = (
            f"In the last {f['window_hours']} hours {whose} moved from {before} to {now} "
            f"({move:+} points). Question: what is the probability that at least half of this "
            f"move is still there {f['horizon_hours']} hours later, at {later}?"
        )
        if framing == "bd":  # the sentence tested in the Phase 4 probe (arm D)
            text += (
                " For reference, the probability implied by the current price that the price ends "
                f"on the side this move points to is {hold_rate_implied(task):.3f}."
            )
        return head + text + " Investigate why it moved, then submit your probability." + cite
    return (
        head
        + (
            # no horizon: a forecaster knows the scheduled end (hist.market), not an early close
            f"The market's price now is {f['price_now']}. Question: what is the probability that "
            "the market's first outcome wins? Investigate, then submit your probability."
        )
        + cite
    )


def facts_receipt(task: Task, frame: Frame) -> Receipt:
    """The numbers the prompt states, as a receipt claims may cite (user decision D2)."""
    f = task.facts
    out: dict[str, Any]
    if task.kind == "spike_hold":
        before, now, move = _shown(task, frame)
        out = {
            "prices_are_for": frame.outcome if frame.flip else "the market's first outcome",
            "price_before": before, "price_now": now, "move_pts": move,
            "window_hours": f["window_hours"], "horizon_hours": f["horizon_hours"],
        }  # fmt: skip
        if frame.framing == "bd":
            out["market_implied_hold_probability"] = round(hold_rate_implied(task), 3)
    else:
        out = {"price_now": f["price_now"]}
    return Receipt.build(
        tool="task.facts", inputs={"task_id": task.task_id}, as_of=task.as_of, output=out,
        data_version=None, effect_size=None, runtime_ms=0, sql=[],
    )  # fmt: skip


OWN_MARKET_TOOLS = {"hist.market", "hist.move", "hist.series"}


def _flip(p: Any) -> Any:
    return None if p is None else round(1.0 - float(p), 6)


def in_frame(r: Receipt, task: Task, frame: Frame) -> Receipt:
    """A tool result as the task is shown: under a flip the own market's prices are the second
    outcome's; other markets keep their first-outcome prices and say so."""
    if not frame.flip:
        return r
    out = dict(r.output)
    effect = r.effect_size
    own = r.inputs.get("condition_id") == task.condition_id
    if r.tool in OWN_MARKET_TOOLS and own:
        if r.tool == "hist.market" and out.get("found"):
            out["price_is_for"] = frame.outcome
        elif r.tool == "hist.move" and out.get("price") is not None:
            out["price"] = _flip(out["price"])
            out["change"] = {k: None if v is None else -v for k, v in out["change"].items()}
            effect = None if effect is None else -effect
            out["prices_are_for"] = frame.outcome
        elif r.tool == "hist.series":
            out["points"] = [[t, _flip(p)] for t, p in out["points"]]
            out["first"] = out["points"][0] if out["points"] else None
            out["last"] = out["points"][-1] if out["points"] else None
            out["prices_are_for"] = frame.outcome
    elif r.tool in ("hist.siblings", "hist.related"):
        out["prices_are_for"] = "each market's first outcome"
    elif r.tool != "hist.market":
        out["prices_are_for"] = "this market's first outcome"
    return Receipt.build(
        tool=r.tool, inputs=r.inputs, as_of=r.as_of, output=out, data_version=r.data_version,
        effect_size=effect, runtime_ms=r.runtime_ms, sql=r.sql,
    )  # fmt: skip


def _second_outcome(conn: duckdb.DuckDBPyConnection, task: Task) -> str:
    try:
        out = run_tool(conn, "hist.market", {"condition_id": task.condition_id}, task.as_of).output
    except ToolError:
        return Frame.outcome
    return str(out["outcomes"][1]) if out.get("found") else Frame.outcome


def _submit_from_text(text: str) -> dict[str, Any] | None:
    """A submit_answer the model wrote as text instead of calling the tool: the first JSON
    object with a probability, bare or under "arguments"."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            inner = obj.get("arguments")
            args: dict[str, Any] = inner if isinstance(inner, dict) else obj
            if "probability" in args:
                return dict(args)
    return None


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
    thinking: str = ""  # reasoning the model wrote before acting (thinking-mode renderers)


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
    parsed_from_text: bool = False  # the answer was written as text, not a submit_answer call
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
    ep.summary = str(args.get("summary") or "")  # reasoning, not a checked claim (D2)
    raw_claims = args.get("claims") or []
    claims = []
    for c in raw_claims if isinstance(raw_claims, list) else []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            ids = [str(r) for r in c.get("receipt_ids") or [] if isinstance(r, str)]
            claims.append(Claim(text=c["text"], receipt_ids=ids))
    concl = Conclusion(case_id=ep.task_id, top_hypothesis=None, ranked=[], claims=claims)
    kept, dropped = verify(concl, receipts, drop_uncited_numbers=True, strict=True)
    ep.claims = kept.claims
    ep.dropped = dropped


def _said(turn: Turn) -> dict[str, str]:
    """What the model wrote besides its tool calls, kept in the transcript for diagnosis."""
    out = {"text": turn.text.strip(), "thinking": turn.thinking.strip()}
    return {k: v for k, v in out.items() if v}


def run_episode(
    model: AgentModel,
    conn: duckdb.DuckDBPyConnection,
    task: Task,
    spend: Spend | None = None,
    framing: str | None = None,
) -> Episode:
    ep = Episode(task_id=task.task_id, model=model.name)
    t0 = time.perf_counter()
    frame = frame_for(task, framing or config.RL_FRAMING)
    if frame.flip:
        frame = replace(frame, outcome=_second_outcome(conn, task))
    facts = facts_receipt(task, frame)
    receipts: dict[str, Receipt] = {facts.receipt_id: facts}
    sess = model.session(SYSTEM, tool_specs())
    user: str | None = prompt_for(task, frame.framing, facts.receipt_id)
    ep.transcript.append({"role": "user", "content": user})
    results: list[tuple[str, str]] = []
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
            said = _said(turn)
            submit = next((c for c in turn.calls if c.name == "submit_answer"), None)
            args = submit.arguments if submit is not None else None
            if args is None and not turn.calls:
                args = _submit_from_text(turn.text)
                ep.parsed_from_text = args is not None
            if args is not None:
                ep.answer_turn = ep.turns - 1
                # the arguments keep the order the model wrote them in (is p written first?)
                ep.transcript.append({"role": "assistant", **said, "submit": args})
                _finish(ep, args, receipts)
                break
            if not turn.calls:
                if said:
                    ep.transcript.append({"role": "assistant", **said})
                if nudged:
                    ep.malformed = True
                    break
                nudged = True
                user = "Call submit_answer now with your probability."
                ep.transcript.append({"role": "user", "content": user})
                continue
            step: dict[str, Any] = {"role": "tool", "calls": [], "results": [], **said}
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
                        r = in_frame(
                            run_tool(conn, TOOL_BY_API[c.name], c.arguments, task.as_of),
                            task,
                            frame,
                        )
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
