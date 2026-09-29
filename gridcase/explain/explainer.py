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

SUBSTANTIAL_COVERAGE = 0.3

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

    # Tiers: a supported data artifact pre-empts everything (broken data explains any miss);
    # then mechanisms that are supported *and substantial* (cover >= SUBSTANTIAL_COVERAGE of
    # the miss, or coverage unknown); then thinly supported and untested ones by confidence
    # (untested ones sit at their prior); refuted last.
    def tier(v: HypothesisVerdict) -> int:
        if v.id == "H0_DATA_ARTIFACT" and v.status == "supported":
            return 0
        if v.status == "refuted":
            return 3
        if v.status == "supported" and (v.coverage is None or v.coverage >= SUBSTANTIAL_COVERAGE):
            return 1
        return 2

    verdicts.sort(key=lambda v: (tier(v), -v.confidence, v.id))
    # A cause pre-empts its consequence only when it is supported *and* substantial: it must
    # account for at least SUBSTANTIAL_COVERAGE of the miss (unknown coverage counts).
    supported = {
        v.id
        for v in verdicts
        if v.status == "supported" and (v.coverage is None or v.coverage >= SUBSTANTIAL_COVERAGE)
    }
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


ENTITY_WORDS = {"ERCO": "ERCOT", "CISO": "CAISO", "PJM": "PJM", "US48": "the US Lower 48"}
METRIC_WORDS = {
    "forecast_error_mw": "the gap between demand and its forecast",
    "forecast_error_pct": "the gap between demand and its forecast",
    "demand": "demand",
    "supply_gap_mw": "the gap between generation and the forecast",
    "net_interchange": "power flowing in and out over the ties",
    "balance_residual": "the supply/demand balance",
    "wind_capacity_factor_proxy": "wind output",
    "solar_capacity_factor_proxy": "solar output",
}


def _who(entity: str) -> str:
    return ENTITY_WORDS.get(entity, entity)


def _what(metric: str) -> str:
    return METRIC_WORDS.get(metric, metric.replace("_", " "))


def _claim_for(r: Receipt) -> Claim | None:
    """One plain-English sentence per receipt. Every number is copied from the receipt so the
    verifier can match it; the wording is for a reader who is not a grid engineer."""
    o = r.output
    t = r.tool
    if o.get("insufficient"):
        until = str(o.get("knowable_until", ""))[:16].replace("T", " ")
        text = (
            f"This check could not see the event hours yet: the data it needs is published with "
            f"a delay and was only available up to {until} UTC."
        )
    elif t == "kpi.alarm":
        if not o.get("alarm"):
            return None
        text = (
            f"This investigation opened automatically: demand in {_who(o['entity'])} ran "
            f"{abs(o['worst_value']):.1f}% {o['direction']} its forecast, more than "
            f"{o['k_sigma']:.0f} standard deviations from its usual {o['mean']:.1f}% for that "
            f"hour of the day over the previous {o['lookback_days']} days (typical spread "
            f"{o['std']:.1f}%), for {o['run_hours']} hours in a row."
        )
    elif t == "anomaly.detect":
        text = (
            f"In {_who(o['entity'])}, {_what(o['metric'])} reached {o['max_abs_z']:.1f} times "
            f"its normal variation; {o['n_flagged_hours']} hours were abnormal and "
            f"{o['missing_hours']} hours of data were missing."
        )
    elif t == "variance.plan_vs_actual":
        text = (
            f"At the worst hour, {_who(o['entity'])} used {abs(o['min_error_mw']):,.0f} MW less "
            f"power than forecast ({abs(o['min_error_pct']):.1f}% of the forecast); over the "
            f"whole window the average shortfall was {abs(o['mean_error_mw']):,.0f} MW."
        )
    elif t == "bridge.fuel":
        top = o["fuels"][0]
        share = (
            f"{o['top_share'] * 100:.0f}% of the movement"
            if o.get("top_share") is not None
            else "an undefined share"
        )
        basis = (
            "compared with what each power source was expected to produce"
            if o.get("mode") == "expected"
            else "between the two hours"
        )
        text = (
            f"Generation in {_who(o['entity'])} moved {o['total_change_mw']:,.0f} MW {basis}; "
            f"{o['top_fuel'].replace('_', ' ')} accounted for {top['delta_mw']:,.0f} MW ({share})."
        )
    elif t == "identity.balance":
        text = (
            f"The numbers for {_who(o['entity'])} add up: demand, generation and imports "
            f"disagree by at most {o['max_abs_z']:.1f} times the usual rounding, with "
            f"{o['n_missing']} missing hours."
        )
    elif t == "revision.diff":
        hist = (
            "though this range was backfilled, so no real revision history exists for it"
            if o["history"] != "observed"
            else "and this revision history is real"
        )
        text = (
            f"{o['n_changed']} values of {
                _what(o['series'].split('.')[-1].lower())
                if o['series'].startswith('eia.')
                else o['series']
            } were changed by later "
            f"revisions, {hist}."
        )
    elif t == "regress.temperature":
        text = (
            f"Temperature explains {o['r2'] * 100:.0f}% of the variation in demand for "
            f"{_who(o['entity'])}; given the weather, demand should have been up to "
            f"{o['implied_minus_actual_mw']:,.0f} MW higher than it actually was."
        )
    elif t == "contribution.subregion":
        text = (
            f"The {o['top_subregion'].split('/')[-1]} zone accounted for "
            f"{o['top_share'] * 100:.0f}% of the change in demand across zones."
        )
    elif t == "peer.compare":
        mc = o.get("max_corr")
        text = (
            f"The most similar neighbouring grid moved with a correlation of {mc:.2f}."
            if mc is not None
            else "No neighbouring grid had enough overlapping data to compare."
        )
    elif t == "price.co_movement":
        text = (
            f"Wholesale prices sat at or near the cap for {o['cap_hours']} hours."
            if o["has_data"]
            else "No wholesale price data was available for this window, so prices could not be "
            "used as evidence."
        )
    elif t == "baseline.expected":
        text = (
            f"The model of a normal hour for {_what(o['metric'])} was fitted on "
            f"{o['n_train']} hours of history and explains {o['r2'] * 100:.0f}% of its variation."
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
