"""Case orchestration: compare -> plan -> execute -> explain -> act, and the later re-check.

Two LLM calls at most per case in the happy path (plan, explain); up to two plan retries.
The re-check is fully deterministic: every step re-runs at the new as_of and falsifiers are
re-applied; no model call.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import duckdb

from gridcase.actions.publish import publish_case
from gridcase.explain.explainer import explain, rank_hypotheses
from gridcase.ledger.schema import Receipt
from gridcase.ledger.store import write_receipt
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
from gridcase.planner.context import BASELINE_DAYS, build_context, case_window
from gridcase.planner.executor import ExecutionResult, evaluate_hypotheses, execute_plan
from gridcase.planner.hypotheses import load_templates
from gridcase.planner.llm import LLM
from gridcase.planner.planner import make_plan
from gridcase.semantic.kpis import Filters, segment_label
from gridcase.tools.base import ToolError, run_tool
from gridcase.trace import Tracer, active_tracer

log = logging.getLogger("gridcase")
PRIOR_SEASON = "all"  # learned priors are kept per (driver, KPI); markets have no seasons


def investigate(
    conn: duckdb.DuckDBPyConnection,
    llm: LLM,
    kpi: str,
    as_of: datetime,
    filters: Filters | None = None,
    trigger: Receipt | None = None,
    dry_run: bool = True,
    window: tuple[datetime, datetime] | None = None,
    on_open: Callable[[str], None] | None = None,
) -> CaseRecord:
    """Why did `kpi` (in the segment `filters`) move? The window is the alarm's run of hours,
    the hours a person picked (`window`, from the Explore page), or else the last complete
    hours before as_of. `on_open` hears the case id as soon as the case exists, so a page can
    follow its progress."""
    filters = dict(filters or {})
    case = new_case(conn, entity=segment_label(filters), metric=kpi, as_of=as_of)
    if on_open is not None:
        on_open(case.case_id)
    tracer = Tracer()
    token = active_tracer.set(tracer)
    try:
        if trigger is not None:  # the alarm that opened this case is its first piece of evidence
            write_receipt(conn, case.case_id, trigger)
        window = window or case_window(as_of, trigger.output if trigger is not None else None)
        with tracer.span("stage", "compare"):
            compare = run_tool(
                conn,
                "kpi.compare",
                {"kpi": kpi, "filters": filters, "window": window, "baseline_days": BASELINE_DAYS},
                as_of,
                case_id=case.case_id,
            )

        priors = {t.id: effective_prior(conn, t.id, kpi, PRIOR_SEASON) for t in load_templates()}
        similar = [
            {"case_id": c.case_id, "as_of": c.as_of.isoformat(), "top_hypothesis": c.top_hypothesis}
            for c in similar_cases(conn, case.entity, kpi, case.season)
        ]
        ctx = build_context(
            conn,
            case.case_id,
            kpi,
            filters,
            as_of,
            window,
            compare.output,
            priors={h: p.mean() for h, p in priors.items()},
            trigger=trigger.output if trigger is not None else None,
            similar_cases=similar,
        )
        with tracer.span("llm", "plan") as detail:
            plan, attempts, usage = make_plan(llm, ctx)
            detail.update(usage.model_dump(), attempts=attempts)
        case.plan, case.plan_attempts = plan, attempts
        case.llm_usage = case.llm_usage + usage
        case.llm_calls += attempts
        save_case(conn, case)
        advance(conn, case.case_id, "planned")

        advance(conn, case.case_id, "executing")
        with tracer.span("stage", "execute"):
            execute_seq = len(tracer.spans) - 1
            result = execute_plan(conn, plan, case.case_id)
            executed = [sid for sid in result.order if result.step_status[sid] != "skipped"]
            checks = [s for s in tracer.spans if s.parent_seq == execute_seq]
            # One run_tool per step that ran; if that ever changes, leave the rows unlabelled.
            if len(checks) == len(executed):
                for span, sid in zip(checks, executed, strict=True):
                    span.detail["step_id"] = sid
        case = get_case(conn, case.case_id)
        case.plan, case.plan_attempts = plan, attempts

        ranked = rank_hypotheses(result, priors, change_size(compare.output))
        with tracer.span("llm", "explain") as detail:
            conclusion, dropped, u2, overruled = explain(
                llm, case.case_id, ranked, case.receipts, ctx.model_dump(mode="json")
            )
            detail.update(u2.model_dump())
            if overruled is not None:  # the ranker's pick stands; keep what the model said
                detail.update(
                    model_top_hypothesis=overruled,
                    top_hypothesis=conclusion.top_hypothesis,
                )
        # explain already verified the claims; record only its returned counts here.
        with tracer.span("stage", "verify") as detail:
            detail.update(
                kept=len(conclusion.claims),
                dropped=len(dropped),
                interpretation=sum(c.tier == "inferred" for c in conclusion.claims),
            )
        usual = within_usual_range(compare.output)
        if usual:
            conclusion = conclusion.model_copy(update={"caveats": [*conclusion.caveats, usual]})
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

        with tracer.span("stage", "publish"):
            case = get_case(conn, case.case_id)
            case.action = publish_case(conn, case, dry_run=dry_run) | {"dropped_claims": dropped}
            save_case(conn, case)
            advance(conn, case.case_id, "action_taken")
            return get_case(conn, case.case_id)
    finally:
        active_tracer.reset(token)
        try:  # a trace must never break, or hide the error of, the investigation it observes
            tracer.save(conn, case.case_id)
        except Exception:
            log.exception("could not save the trace of case %s", case.case_id)


def within_usual_range(o: dict[str, Any]) -> str | None:
    """A note when the KPI sits inside the range of the same hours on previous days: there may
    be nothing to explain (a person can ask about any window, not only alarmed ones)."""
    lo, hi, now = o.get("lowest_past"), o.get("highest_past"), o.get("now")
    if o.get("insufficient") or lo is None or hi is None or now is None or not lo <= now <= hi:
        return None
    return (
        f"These hours are within the usual range: the same hours on each of the previous "
        f"{o['baseline_days_used']} days ranged from {lo:,.2f} to {hi:,.2f}, and now it is "
        f"{now:,.2f}. There may be nothing unusual to explain."
    )


def change_size(compare_output: dict[str, Any]) -> float:
    """How big the change was (what the drivers' `explains` fields are divided by)."""
    return abs(float(compare_output.get("change") or 0.0))


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

    priors = {
        t.id: effective_prior(conn, t.id, case.metric, PRIOR_SEASON) for t in load_templates()
    }
    first = next((r for r in case.receipts if r.tool == "kpi.compare"), None)
    ranked = rank_hypotheses(result, priors, change_size(first.output) if first else 0.0)
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
