"""Rewrite a case from saved evidence only. Stop gridcase-server first: it is the
store's only writer. Dry runs use a read-only connection supplied by the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import duckdb

from gridcase.explain.explainer import explain, rank_hypotheses
from gridcase.explain.schema import Conclusion, HypothesisVerdict
from gridcase.investigate import change_size, explanation_caveats
from gridcase.ledger.schema import Receipt, canonical_json
from gridcase.memory.cases import CaseRecord, get_case
from gridcase.planner.context import BASELINE_DAYS, case_window
from gridcase.planner.executor import ExecutionResult, evaluate_hypotheses, topological
from gridcase.planner.hypotheses import load_templates, prior_for
from gridcase.planner.llm import LLM, LLMUsage
from gridcase.tools.registry import ALL_INPUTS
from gridcase.trace import Tracer


@dataclass
class Reexplanation:
    conclusion: Conclusion
    dropped: list[str]
    usage: LLMUsage


def original_receipts(case: CaseRecord) -> list[Receipt]:
    """Exclude evidence appended by the later, deterministic recheck."""
    old = (case.recheck or {}).get("old_receipts")
    if old is not None:  # the recheck recorded exactly which receipts were the original ones
        old_ids = set(old)
        return [r for r in case.receipts if r.receipt_id in old_ids]
    # The alarm receipt keeps full precision while case.as_of is cut to the minute, and a
    # recheck runs hours later, so everything before the next minute is original evidence.
    cutoff = case.as_of + timedelta(minutes=1)
    return [r for r in case.receipts if r.as_of < cutoff]


def rebuild_ranking(
    case: CaseRecord, receipts: list[Receipt], change: dict[str, Any]
) -> list[HypothesisVerdict]:
    """Replay only falsifier arithmetic, never tools or live priors. Stored ranked verdicts
    are authoritative; this is for older cases that lack them. Current ontology priors are
    the only available fallback because original effective priors were not persisted.
    """
    if case.conclusion is not None and case.conclusion.ranked:
        return case.conclusion.ranked
    assert case.plan is not None
    result = ExecutionResult(hypothesis_status={h: "open" for h in case.plan.hypotheses})
    for step in topological(case.plan.steps):
        if step.hypothesis_ids and all(
            result.hypothesis_status.get(h) == "refuted" for h in step.hypothesis_ids
        ):
            continue
        if step.step_id in case.step_errors:
            continue
        inputs = ALL_INPUTS[step.tool].model_validate(step.inputs).model_dump(mode="json")
        receipt = next(
            (
                r
                for r in receipts
                if r.tool == step.tool and canonical_json(r.inputs) == canonical_json(inputs)
            ),
            None,
        )
        if receipt is None:
            raise ValueError(f"cannot reconstruct ranking: no stored receipt for {step.step_id}")
        result.step_outputs[step.step_id] = receipt.output
        evaluate_hypotheses(case.plan, result)
    evaluate_hypotheses(case.plan, result)
    priors = {t.id: prior_for(t.id, case.metric, "all") for t in load_templates()}
    return rank_hypotheses(result, priors, change_size(change))


def stored_context(case: CaseRecord, receipts: list[Receipt], compare: Receipt) -> dict[str, Any]:
    """The explain subset of build_context's snapshot, without querying trades, similar
    cases or learned priors that may have changed since the original investigation.
    Unknown original values are explicit rather than presented as reconstructed facts.
    """
    assert case.plan is not None
    trigger = next((r.output for r in receipts if r.tool == "kpi.alarm"), {})
    raw_window = compare.inputs.get("window") or compare.output.get("window")
    window = (
        tuple(datetime.fromisoformat(v) for v in raw_window)
        if raw_window
        else case_window(case.as_of, trigger)
    )
    notes = [
        "Original record_days, effective priors and similar_cases were not stored "
        "and are unavailable.",
        "Applicable drivers are the saved plan's hypotheses; other originally applicable "
        "drivers are unknown.",
    ]
    if not case.conclusion or not case.conclusion.ranked:
        notes.append(
            "Ranking recomputed from saved receipts with current falsifiers and ontology "
            "priors; original learned priors are unavailable."
        )
    return {
        "case_id": case.case_id,
        "kpi": case.metric,
        "filters": compare.inputs.get("filters", compare.output.get("filters", {})),
        "as_of": case.as_of.isoformat(),
        "window": [v.isoformat() for v in window],
        "baseline_days": compare.inputs.get("baseline_days", BASELINE_DAYS),
        "change": compare.output,
        "trigger": trigger,
        "record_days": None,
        "applicable": case.plan.hypotheses,
        "priors": {},
        "similar_cases": [],
        "reconstruction_notes": notes,
    }


def reexplain_case(
    conn: duckdb.DuckDBPyConnection, llm: LLM, case_id: str, *, dry_run: bool = False
) -> Reexplanation:
    """Rewrite only the conclusion and usage; append an audit span atomically. Stop
    gridcase-server first. A missing model answer raises TemplateFallback without writes.
    """
    case = get_case(conn, case_id)
    if case.plan is None:
        raise ValueError("case has no stored plan")
    receipts = original_receipts(case)
    compare = next((r for r in receipts if r.tool == "kpi.compare"), None)
    if compare is None:
        raise ValueError("case has no original comparison receipt")
    context = stored_context(case, receipts, compare)
    ranked = rebuild_ranking(case, receipts, compare.output)
    tracer = Tracer()
    calls_before = llm.calls
    with tracer.span("llm", "re-explain") as detail:
        conclusion, dropped, usage, overruled = explain(
            llm, case_id, ranked, receipts, context, require_model=True
        )
        detail.update(usage.model_dump())
        detail.update(
            model=usage.model or getattr(llm, "model", type(llm).__name__),
            kept=len(conclusion.claims),
            dropped=len(dropped),
            reconstruction_notes=context["reconstruction_notes"],
        )
        if overruled is not None:
            detail.update(model_top_hypothesis=overruled, top_hypothesis=conclusion.top_hypothesis)
    conclusion = explanation_caveats(conclusion, compare.output, case.plan, case.step_errors)
    if not conclusion.claims:  # the verifier kept nothing: the template is better than empty
        raise ValueError(f"model summary kept no verified claims ({len(dropped)} dropped)")
    if not dry_run:
        # Update only these columns: even closed cases may have a newer top_hypothesis
        # from their recheck, which is distinct from the original conclusion's ranking.
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                "UPDATE cases SET conclusion_json = ?, llm_usage_json = ?, llm_calls = ? "
                "WHERE case_id = ?",
                [
                    conclusion.model_dump_json(),
                    (case.llm_usage + usage).model_dump_json(),
                    case.llm_calls + llm.calls - calls_before,
                    case_id,
                ],
            )
            row = conn.execute(
                "SELECT coalesce(max(seq), -1) + 1 FROM case_trace WHERE case_id = ?", [case_id]
            ).fetchone()
            assert row is not None
            tracer.spans[0].seq = row[0]
            tracer.save(conn, case_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return Reexplanation(conclusion, dropped, usage)
