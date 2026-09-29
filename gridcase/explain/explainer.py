"""Turn receipts and hypothesis verdicts into a Conclusion.

`template_conclusion` writes one claim per receipt from fixed sentence templates whose
numbers come straight from the receipt, so it always passes the verifier; it is the FakeLLM's
explainer and the fallback when the model call fails. `explain` asks the LLM, then verifies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from gridcase.explain.confidence import confidence
from gridcase.explain.schema import Claim, Conclusion, HypothesisVerdict, Status
from gridcase.explain.verifier import verify
from gridcase.ledger.schema import Receipt
from gridcase.planner.hypotheses import Prior, load_templates

if TYPE_CHECKING:
    from gridcase.planner.executor import ExecutionResult
    from gridcase.planner.llm import LLM, LLMUsage

SERVED_LOAD_CAVEAT = (
    "EIA demand during a load-shed event is served load, not true demand: a fall in reported "
    "demand does not mean consumers wanted less power."
)


def rank_hypotheses(
    result: ExecutionResult, priors: dict[str, Prior], miss_mw: float | None = None
) -> list[HypothesisVerdict]:
    """Confidence from prior, falsifier margin and coverage of the miss; then causal
    precedence: a supported mechanism that is a consequence of another supported mechanism
    ranks below it (load shed follows from a supply shortfall, not the other way round)."""
    templates = {t.id: t for t in load_templates()}
    verdicts: list[HypothesisVerdict] = []
    for h, status in result.hypothesis_status.items():
        m = result.margins.get(h)
        cov = result.coverage(h, miss_mw) if miss_mw else None
        conf = confidence(priors[h], m, cov)
        verdicts.append(
            HypothesisVerdict(
                id=h, status=cast(Status, status), confidence=conf, margin=m, coverage=cov
            )
        )
    verdicts.sort(key=lambda v: (v.status == "refuted", -v.confidence, v.id))
    supported = {v.id for v in verdicts if v.status == "supported"}
    ordered: list[HypothesisVerdict] = []
    deferred: list[HypothesisVerdict] = []
    for v in verdicts:
        causes = (
            [c for c in templates[v.id].consequence_of if c in supported]
            if v.id in templates
            else []
        )
        (deferred if causes else ordered).append(v)
    for v in deferred:  # place each consequence right after the last of its supported causes
        cause_ids = set(templates[v.id].consequence_of) & supported
        idx = max((i for i, o in enumerate(ordered) if o.id in cause_ids), default=-1)
        pos = idx + 1
        while (
            pos < len(ordered)
            and ordered[pos].status != "refuted"
            and ordered[pos].confidence >= v.confidence
        ):
            pos += 1
        ordered.insert(pos, v)
    return ordered


def _claim_for(r: Receipt) -> Claim | None:
    o = r.output
    t = r.tool
    if t == "anomaly.detect":
        text = (
            f"{o['metric']} for {o['entity']} reached |z| = {o['max_abs_z']:.1f} against its "
            f"baseline; {o['n_flagged_hours']} hours were flagged and {o['missing_hours']} hours "
            "were missing."
        )
    elif t == "variance.plan_vs_actual":
        text = (
            f"Demand for {o['entity']} was {o['min_error_mw']:,.0f} MW below forecast at the "
            f"worst hour ({o['min_error_pct']:.1f}% of forecast); the mean error over the window "
            f"was {o['mean_error_mw']:,.0f} MW."
        )
    elif t == "bridge.fuel":
        top = o["fuels"][0]
        share = (
            f"{o['top_share'] * 100:.0f}%"
            if o.get("top_share") is not None
            else "an undefined share"
        )
        basis = (
            "against each fuel's expected output"
            if o.get("mode") == "expected"
            else "between the two hours"
        )
        text = (
            f"Net generation for {o['entity']} was {o['total_change_mw']:,.0f} MW {basis}; "
            f"{o['top_fuel']} accounted for {top['delta_mw']:,.0f} MW ({share} of the movement)."
        )
    elif t == "identity.balance":
        text = (
            f"The balance identity for {o['entity']} had a maximum |z| of {o['max_abs_z']:.1f} "
            f"with {o['n_missing']} missing hours."
        )
    elif t == "revision.diff":
        hist = (
            "no real revision history exists for this range"
            if o["history"] != "observed"
            else "revision history is real"
        )
        text = (
            f"{o['n_changed']} rows of {o['series']} were revised between the two vintages; {hist}."
        )
    elif t == "regress.temperature":
        text = (
            f"Temperature explained R² = {o['r2']:.2f} of demand for {o['entity']}; "
            f"temperature-implied demand exceeded actual by up to "
            f"{o['implied_minus_actual_mw']:,.0f} MW."
        )
    elif t == "contribution.subregion":
        text = (
            f"{o['top_subregion']} carried {o['top_share'] * 100:.0f}% of the change in demand "
            "across subregions."
        )
    elif t == "peer.compare":
        mc = o.get("max_corr")
        text = (
            f"The forecast error's highest correlation with a peer BA was {mc:.2f}."
            if mc is not None
            else "No peer BA had enough overlapping data to correlate forecast errors."
        )
    elif t == "price.co_movement":
        text = (
            f"Hub prices were at or near the cap for {o['cap_hours']} hours."
            if o["has_data"]
            else "No hub price data was available for the window, so price evidence is absent."
        )
    elif t == "baseline.expected":
        text = (
            f"The baseline model for {o['metric']} was fit on {o['n_train']} hours with "
            f"R² = {o['r2']:.2f}."
        )
    else:
        return None
    return Claim(text=text, receipt_ids=[r.receipt_id], tier="governed")


def template_conclusion(
    case_id: str,
    ranked: list[HypothesisVerdict],
    receipts: list[Receipt],
    context: dict[str, Any] | None = None,
) -> Conclusion:
    claims = [c for c in (_claim_for(r) for r in receipts) if c is not None]
    top = next((v.id for v in ranked if v.status != "refuted"), None)
    caveats: list[str] = []
    status = {v.id: v.status for v in ranked}
    if status.get("H4_LOAD_SHED") not in (None, "refuted"):
        caveats.append(SERVED_LOAD_CAVEAT)
    if top is not None:
        claims.append(
            Claim(
                text=f"The best-supported explanation is {top}; alternatives are listed in "
                "rank order.",
                receipt_ids=[],
                tier="inferred",
            )
        )
    return Conclusion(
        case_id=case_id, top_hypothesis=top, ranked=ranked, claims=claims, caveats=caveats
    )


def explain(
    llm: LLM,
    case_id: str,
    ranked: list[HypothesisVerdict],
    receipts: list[Receipt],
    context: dict[str, Any],
) -> tuple[Conclusion, list[str], LLMUsage]:
    conclusion, usage = llm.explain(case_id, ranked, receipts, context)
    if conclusion is None:
        conclusion = template_conclusion(case_id, ranked, receipts, context)
    conclusion = conclusion.model_copy(update={"case_id": case_id, "ranked": ranked})
    if conclusion.top_hypothesis is None:
        conclusion.top_hypothesis = next((v.id for v in ranked if v.status != "refuted"), None)
    verified, dropped = verify(conclusion, {r.receipt_id: r for r in receipts})
    return verified, dropped, usage
