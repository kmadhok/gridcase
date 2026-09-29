"""Case orchestration: detect -> plan -> execute -> explain -> act, and the later re-check.

Two LLM calls at most per case in the happy path (plan, explain); up to two plan retries.
The re-check is fully deterministic: every step re-runs at the new as_of and falsifiers are
re-applied; no model call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import duckdb

from gridcase.actions.publish import publish_case
from gridcase.explain.explainer import explain, rank_hypotheses
from gridcase.memory.cases import (
    CaseRecord,
    advance,
    close_case,
    effective_prior,
    get_case,
    new_case,
    save_case,
    similar_cases,
)
from gridcase.planner.context import WINDOW_HOURS, build_context
from gridcase.planner.executor import ExecutionResult, evaluate_hypotheses, execute_plan
from gridcase.planner.hypotheses import load_templates
from gridcase.planner.llm import LLM
from gridcase.planner.planner import make_plan
from gridcase.tools.base import ToolError, run_tool


def investigate(
    conn: duckdb.DuckDBPyConnection,
    llm: LLM,
    entity: str,
    metric: str,
    as_of: datetime,
    dry_run: bool = True,
) -> CaseRecord:
    case = new_case(conn, entity=entity, metric=metric, as_of=as_of)
    window = (as_of - timedelta(hours=WINDOW_HOURS), as_of)
    anomaly = run_tool(
        conn,
        "anomaly.detect",
        {"metric": metric, "entity": entity, "window": window},
        as_of,
        case_id=case.case_id,
    )

    priors = {t.id: effective_prior(conn, t.id, entity, case.season) for t in load_templates()}
    similar = [
        {"case_id": c.case_id, "as_of": c.as_of.isoformat(), "top_hypothesis": c.top_hypothesis}
        for c in similar_cases(conn, entity, metric, case.season)
    ]
    ctx = build_context(
        conn,
        case.case_id,
        entity,
        metric,
        as_of,
        anomaly.output,
        priors={h: p.mean() for h, p in priors.items()},
        similar_cases=similar,
    )
    plan, attempts, usage = make_plan(llm, ctx)
    case.plan, case.plan_attempts = plan, attempts
    case.llm_usage = case.llm_usage + usage
    case.llm_calls += attempts if plan.source == "llm" or attempts > 1 else attempts
    save_case(conn, case)
    advance(conn, case.case_id, "planned")

    advance(conn, case.case_id, "executing")
    result = execute_plan(conn, plan, case.case_id)
    case = get_case(conn, case.case_id)
    case.plan, case.plan_attempts = plan, attempts

    miss_mw = float(anomaly.output.get("max_abs_dev") or 0.0)
    ranked = rank_hypotheses(result, priors, miss_mw)
    conclusion, dropped, u2 = explain(
        llm, case.case_id, ranked, case.receipts, ctx.model_dump(mode="json")
    )
    case.step_errors = dict(result.errors)
    if result.errors:
        failed = ", ".join(
            f"{sid} ({next((s.tool for s in plan.steps if s.step_id == sid), '?')}): {msg[:80]}"
            for sid, msg in result.errors.items()
        )
        note = (
            f"{len(result.errors)} of {len(plan.steps)} planned steps failed and produced no "
            f"evidence: {failed}"
        )
        conclusion = conclusion.model_copy(update={"caveats": [*conclusion.caveats, note]})
    case.conclusion = conclusion
    case.top_hypothesis = conclusion.top_hypothesis
    case.llm_usage = case.llm_usage + u2
    case.llm_calls = llm.calls
    case.recheck = None
    save_case(conn, case)
    advance(conn, case.case_id, "concluded")

    case = get_case(conn, case.case_id)
    case.action = publish_case(conn, case, dry_run=dry_run) | {"dropped_claims": dropped}
    save_case(conn, case)
    advance(conn, case.case_id, "action_taken")
    return get_case(conn, case.case_id)


@dataclass
class RecheckResult:
    verdicts: dict[str, str]
    diffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    result: ExecutionResult | None = None


def recheck(conn: duckdb.DuckDBPyConnection, case_id: str, as_of: datetime) -> RecheckResult:
    case = get_case(conn, case_id)
    if case.plan is None or case.conclusion is None:
        raise ValueError("case has no plan/conclusion to re-check")
    if case.state in ("action_taken", "concluded"):
        if case.state == "concluded":
            advance(conn, case_id, "action_taken")
        advance(conn, case_id, "rechecking")
    plan = case.plan.model_copy(update={"as_of": as_of})
    result = ExecutionResult()
    old_by_step = {}
    old_receipts = {r.receipt_id: r for r in case.receipts}
    for step in plan.steps:
        try:
            r = run_tool(conn, step.tool, step.inputs, as_of, case_id=case_id)
        except ToolError as e:
            result.step_status[step.step_id] = "failed"
            result.errors[step.step_id] = str(e)
            continue
        result.receipts.append(r)
        result.step_status[step.step_id] = "ran"
        result.step_outputs[step.step_id] = r.output
        result.outputs[step.tool] = r.output
        old_by_step[step.step_id] = r
    for h in plan.hypotheses:
        result.hypothesis_status[h] = "open"
    evaluate_hypotheses(plan, result)

    priors = {t.id: effective_prior(conn, t.id, case.entity, case.season) for t in load_templates()}
    first = next((r for r in case.receipts if r.tool == "anomaly.detect"), None)
    miss_mw = float(first.output.get("max_abs_dev") or 0.0) if first else 0.0
    ranked = rank_hypotheses(result, priors, miss_mw)
    new_top = next((v.id for v in ranked if v.status != "refuted"), None)
    verdicts: dict[str, str] = {}
    for h in plan.hypotheses:
        truth = result.evaluation.get(h)
        if truth is True:
            verdicts[h] = "refuted"
        elif truth is False:
            verdicts[h] = (
                "superseded" if (h == case.top_hypothesis and new_top != h) else "confirmed"
            )
        else:
            verdicts[h] = "open"
    diffs = {
        h: {
            "before": next((v.status for v in case.conclusion.ranked if v.id == h), None),
            "after": verdicts[h],
        }
        for h in plan.hypotheses
    }
    case = get_case(conn, case_id)
    case.recheck = {
        "as_of": as_of.isoformat(),
        "verdicts": verdicts,
        "diffs": diffs,
        "new_top": new_top,
        "old_receipts": sorted(old_receipts),
        "new_receipts": [r.receipt_id for r in result.receipts],
    }
    case.top_hypothesis = new_top
    save_case(conn, case)
    close_case(conn, case_id, verdicts)
    return RecheckResult(verdicts=verdicts, diffs=diffs, result=result)
