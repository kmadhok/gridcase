"""Answer one question: let the asker read the context and run up to MAX_CHECKS read-only
checks at the case's as_of, then verify every sentence against the evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import duckdb
from pydantic import ValidationError

from gridcase.ask.schema import Answer
from gridcase.explain.schema import Claim, Conclusion
from gridcase.explain.verifier import verify
from gridcase.ledger.schema import Receipt
from gridcase.memory.cases import CaseRecord
from gridcase.planner.llm import LLMUsage
from gridcase.planner.llm_schema import LLM_INPUTS, tool_inputs
from gridcase.tools.base import ToolError, run_tool

MAX_CHECKS = 3

RunCheck = Callable[[str, dict[str, Any]], Receipt]


class Asker(Protocol):
    calls: int

    def ask(
        self, question: str, context: dict[str, Any], run_check: RunCheck, checks_enabled: bool
    ) -> tuple[Answer | None, LLMUsage]: ...


@dataclass
class AskResult:
    sentences: list[Claim]
    refused: bool
    new_receipts: list[Receipt] = field(default_factory=list)
    dropped: int = 0
    usage: LLMUsage = field(default_factory=LLMUsage)


def case_checker(conn: duckdb.DuckDBPyConnection, case: CaseRecord) -> RunCheck:
    """Run a check exactly as the investigation could have: at the case's as_of, read-only
    (no case id, so nothing is written to the ledger)."""

    def run(tool: str, raw: dict[str, Any]) -> Receipt:
        if tool not in LLM_INPUTS:
            raise ToolError(f"unknown check {tool}")
        try:
            inputs = tool_inputs(tool, raw)
        except ValidationError as e:
            raise ToolError(f"invalid inputs for {tool}: {e.errors()[0]['msg']}") from e
        return run_tool(conn, tool, inputs, case.as_of)

    return run


def answer_question(
    asker: Asker,
    question: str,
    *,
    context: dict[str, Any],
    receipts: dict[str, Receipt],
    run_check: RunCheck | None,
    max_checks: int = MAX_CHECKS,
) -> AskResult:
    new: list[Receipt] = []

    def limited(tool: str, raw: dict[str, Any]) -> Receipt:
        if run_check is None:
            raise ToolError("checks are not available on this page")
        if len(new) >= max_checks:
            raise ToolError(f"at most {max_checks} checks per question")
        rec = run_check(tool, raw)
        new.append(rec)
        return rec

    answer, usage = asker.ask(question, context, limited, run_check is not None)
    if answer is None:
        note = Claim(text="Sorry, I couldn't put together an answer just now.", tier="inferred")
        return AskResult([note], False, new, 0, usage)
    pool = {**receipts, **{r.receipt_id: r for r in new}}
    checked, dropped = verify(
        Conclusion(case_id="ask", top_hypothesis=None, ranked=[], claims=answer.sentences),
        pool,
        drop_uncited_numbers=True,
    )
    return AskResult(checked.claims, answer.refused, new, len(dropped), usage)
