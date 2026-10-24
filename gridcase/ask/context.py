"""What the question box knows: the same structured material the planner and explainer used,
in plain names, without the SQL (which stays in the UI)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from gridcase.ask.glossary import CAUSES, CHECKS, GLOSSARY, STATUS
from gridcase.ledger.schema import Receipt
from gridcase.memory.cases import CaseRecord
from gridcase.planner.hypotheses import load_templates

CODE_LEN = 8


def cite_code(receipt_id: str) -> str:
    """The short code the model cites; `engine.resolve_citations` maps it back."""
    return receipt_id[:CODE_LEN]


def case_context(case: CaseRecord) -> dict[str, Any]:
    templates = {t.id: t for t in load_templates()}
    ranked = case.conclusion.ranked if case.conclusion else []
    causes = []
    for v in ranked:
        name, blurb = CAUSES.get(v.id, (v.id, ""))
        t = templates.get(v.id)
        causes.append(
            {
                "id": v.id,
                "name": name,
                "what_it_means": blurb,
                "status": STATUS.get(v.status, v.status),
                "likely": v.confidence,
                "explains_share_of_gap": v.coverage,
                "mechanism": t.mechanism if t else "",
                "what_rules_it_out": t.falsifier if t else "",
            }
        )
    plan = [
        {
            "step": s.step_id,
            "check": CHECKS.get(s.tool, s.tool),
            "tool": s.tool,
            "inputs": s.inputs,
            "outcome": "failed: " + case.step_errors[s.step_id]
            if s.step_id in case.step_errors
            else "ran or skipped",
        }
        for s in (case.plan.steps if case.plan else [])
    ]
    evidence = [
        {
            "cite_as": cite_code(r.receipt_id),
            "check": CHECKS.get(r.tool, r.tool),
            "tool": r.tool,
            "inputs": r.inputs,
            "output": r.output,
        }
        for r in case.receipts
    ]
    return {
        "page": "investigation",
        "kpi": case.metric,
        "segment": case.entity,
        "question_investigated": (
            f"why {case.metric} ({case.entity}) moved away from its usual level"
        ),
        "point_in_time": case.as_of.isoformat(),
        "best_supported_cause": CAUSES.get(case.top_hypothesis or "", (None, ""))[0],
        "causes": causes,
        "plan": plan,
        "evidence": evidence,
        "conclusion_sentences": [
            {"text": c.text, "cites": [cite_code(rid) for rid in c.receipt_ids]}
            for c in (case.conclusion.claims if case.conclusion else [])
        ],
        "caveats": case.conclusion.caveats if case.conclusion else [],
        "follow_up": case.recheck,
        "glossary": GLOSSARY,
    }


def now_context(conn: Any, as_of: datetime) -> tuple[dict[str, Any], dict[str, Receipt]]:
    """The Now page as citable evidence: each team KPI over the last six complete hours vs the
    same hours on previous days, the category breakdown of volume, each watched series' status
    and the latest investigations."""
    from gridcase.market_alarm import check_series, load_watch
    from gridcase.memory.retractions import public_cases
    from gridcase.planner.context import BASELINE_DAYS, case_window
    from gridcase.semantic.kpis import load_kpis
    from gridcase.tools.base import run_tool

    watched, _ = load_watch()
    kpis, _ = load_kpis()
    window = case_window(as_of, None)
    receipts: dict[str, Receipt] = {}
    for w in watched:
        if not w.filters:
            r = run_tool(
                conn,
                "kpi.compare",
                {"kpi": w.kpi, "window": window, "baseline_days": BASELINE_DAYS},
                as_of,
            )
            receipts[r.receipt_id] = r
    r = run_tool(
        conn,
        "by.category",
        {"kpi": "volume_usd", "window": window, "baseline_days": BASELINE_DAYS},
        as_of,
    )
    receipts[r.receipt_id] = r
    series = []
    for w in watched:
        o = check_series(conn, w, as_of).output
        series.append(
            {
                "series": w.label,
                "kpi": kpis[w.kpi].label,
                "segment": w.segment,
                "status": o["status"],
                "times_usual": o["ratio"],
                "value": o["value"],
                "usual": o["expected"],
            }
        )
    cases = [
        {
            "kpi": c.metric,
            "segment": c.entity,
            "as_of": c.as_of.isoformat(),
            "best_supported_cause": CAUSES.get(c.top_hypothesis or "", (None, ""))[0],
        }
        for c in public_cases(conn)[:5]  # as the Now page lists them
    ]
    evidence = [
        {
            "cite_as": cite_code(r.receipt_id),
            "check": CHECKS.get(r.tool, r.tool),
            "tool": r.tool,
            "inputs": r.inputs,
            "output": r.output,
        }
        for r in receipts.values()
    ]
    ctx = {
        "page": "now",
        "point_in_time": as_of.isoformat(),
        "window": [window[0].isoformat(), window[1].isoformat()],
        "kpis": {k: {"label": v.label, "description": v.description} for k, v in kpis.items()},
        "series": series,
        "recent_investigations": cases,
        "evidence": evidence,
        "glossary": GLOSSARY,
    }
    return ctx, receipts
