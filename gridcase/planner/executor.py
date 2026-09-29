"""Executor: run the plan's DAG in topological order, one receipt per step, falsifiers after
every step; skip steps whose hypotheses are all refuted; a failing tool fails only its step.

Falsifiers read the outputs of the steps that test that hypothesis. When two such steps use
the same tool (e.g. H6's wind and solar anomaly checks), numeric fields merge by largest
magnitude ("most anomalous wins") and other fields take the later step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import duckdb

from gridcase.ledger.schema import Receipt
from gridcase.planner.falsifier import evaluate, margin, parse
from gridcase.planner.hypotheses import load_templates
from gridcase.planner.schema import InvestigationPlan, ToolStep
from gridcase.tools.base import ToolError, run_tool


@dataclass
class ExecutionResult:
    receipts: list[Receipt] = field(default_factory=list)
    step_status: dict[str, str] = field(default_factory=dict)  # ran | skipped | failed
    hypothesis_status: dict[str, str] = field(default_factory=dict)  # open | refuted
    evaluation: dict[str, bool | None] = field(default_factory=dict)  # falsifier truth
    margins: dict[str, float | None] = field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)  # by tool, last wins
    step_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    hypothesis_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def coverage(self, hid: str, miss_mw: float) -> float | None:
        """Share of the miss the hypothesis' `explains` field accounts for, if measurable."""
        from gridcase.planner.falsifier import _lookup
        from gridcase.planner.hypotheses import template_by_id

        try:
            path = template_by_id(hid).explains
        except KeyError:
            return None
        if path is None:
            return 1.0
        v = _lookup(self.hypothesis_outputs.get(hid, {}), path)
        if v is None or miss_mw <= 0:
            return None
        try:
            return min(1.0, abs(float(v)) / miss_mw)
        except (TypeError, ValueError):
            return None


def topological(steps: list[ToolStep]) -> list[ToolStep]:
    by_id = {s.step_id: s for s in steps}
    done: set[str] = set()
    out: list[ToolStep] = []
    remaining = list(steps)
    while remaining:
        ready = [s for s in remaining if all(d in done or d not in by_id for d in s.depends_on)]
        if not ready:
            raise ValueError("cycle in plan")
        s = ready[0]
        out.append(s)
        done.add(s.step_id)
        remaining.remove(s)
    return out


def merge_outputs(
    steps: list[ToolStep], step_outputs: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for s in steps:
        out = step_outputs.get(s.step_id)
        if out is None or out.get("insufficient"):
            continue  # a tool whose horizon does not reach the event leaves things unknown
        cur = merged.setdefault(s.tool, {})
        for k, v in out.items():
            if (
                isinstance(v, int | float)
                and not isinstance(v, bool)
                and isinstance(cur.get(k), int | float)
            ):
                cur[k] = v if abs(v) >= abs(cur[k]) else cur[k]
            else:
                cur[k] = v
    return merged


def evaluate_hypotheses(plan: InvestigationPlan, result: ExecutionResult) -> None:
    templates = {t.id: t for t in load_templates()}
    for h in plan.hypotheses:
        t = templates.get(h)
        if t is None:
            continue
        mine = [s for s in plan.steps if h in s.hypothesis_ids]
        outputs = merge_outputs(mine, result.step_outputs)
        rule = parse(t.falsifier)
        truth = evaluate(rule, outputs)
        result.evaluation[h] = truth
        result.margins[h] = None if truth is None else -margin(rule, outputs)
        result.hypothesis_status[h] = (
            "refuted" if truth is True else "supported" if truth is False else "open"
        )
        result.hypothesis_outputs[h] = outputs


def execute_plan(
    conn: duckdb.DuckDBPyConnection, plan: InvestigationPlan, case_id: str
) -> ExecutionResult:
    result = ExecutionResult()
    for h in plan.hypotheses:
        result.hypothesis_status[h] = "open"
    for step in topological(plan.steps):
        result.order.append(step.step_id)
        if step.hypothesis_ids and all(
            result.hypothesis_status.get(h) == "refuted" for h in step.hypothesis_ids
        ):
            result.step_status[step.step_id] = "skipped"
            continue
        try:
            r = run_tool(conn, step.tool, step.inputs, plan.as_of, case_id=case_id)
        except ToolError as e:
            result.step_status[step.step_id] = "failed"
            result.errors[step.step_id] = str(e)
            continue
        result.receipts.append(r)
        result.step_status[step.step_id] = "ran"
        result.step_outputs[step.step_id] = r.output
        result.outputs[step.tool] = r.output
        evaluate_hypotheses(plan, result)
    evaluate_hypotheses(plan, result)
    return result
