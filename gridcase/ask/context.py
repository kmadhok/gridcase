"""What the question box knows: the same structured material the planner and explainer used,
in plain names, without the SQL (which stays in the UI)."""

from __future__ import annotations

from typing import Any

from gridcase.ask.glossary import CAUSES, CHECKS, GLOSSARY, STATUS
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
