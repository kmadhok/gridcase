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
from gridcase.planner.hypotheses import Prior, load_templates, mandatory_ids
from gridcase.semantic.kpis import Filters, load_kpis

if TYPE_CHECKING:
    from gridcase.planner.executor import ExecutionResult
    from gridcase.planner.llm import LLM, LLMUsage

SUBSTANTIAL_COVERAGE = 0.3


def rank_hypotheses(
    result: ExecutionResult, priors: dict[str, Prior], miss_mw: float | None = None
) -> list[HypothesisVerdict]:
    """Confidence from prior, falsifier margin and coverage of the miss; then causal
    precedence: a supported mechanism that is a consequence of another supported mechanism
    ranks below it (load shed follows from a supply shortfall, not the other way round)."""
    templates = {t.id: t for t in load_templates()}
    mandatory = set(mandatory_ids())
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
        if v.id in mandatory and v.status == "supported":
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


DRIVER_WORDS = {
    "D0_DATA_PROBLEM": "a problem with our data",
    "D1_ONE_MARKET": "one market",
    "D2_ONE_CATEGORY": "one category",
    "D3_NEW_TRADERS": "new traders",
    "D4_WHALES": "a few large trades",
    "D5_LONGSHOTS": "bets on long shots",
    "D6_BROAD_BASED": "a broad shift across many markets",
}
DIM_WORDS = {
    "market": "market",
    "category": "category",
    "size_band": "trade size",
    "trader_type": "trader",
    "odds_band": "odds",
    "market_kind": "market kind",
}


def _kpi_words(kpi: str, filters: Filters | None) -> str:
    kpis, _ = load_kpis()
    label = kpis[kpi].label.lower() if kpi in kpis else kpi.replace("_", " ")

    def words(v: str | list[str]) -> str:
        return v if isinstance(v, str) else " or ".join(v)

    seg = ", ".join(
        words(v) if k == "category" else f"{DIM_WORDS.get(k, k)} {words(v)}"
        for k, v in sorted((filters or {}).items())
    )
    return f"{label} in {seg}" if seg else label


def _amount(kpi: str, v: float | None) -> str:
    if v is None:
        return "unknown"
    kpis, _ = load_kpis()
    unit = kpis[kpi].unit if kpi in kpis else ""
    if unit == "usd":
        return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"
    if unit == "share":
        return f"{100 * v:.0f}%"
    return f"{v:,.0f}"


def _claim_for(r: Receipt) -> Claim | None:
    """One plain-English sentence per receipt. Every number is copied from the receipt so the
    verifier can match it; the wording is for a product manager."""
    o = r.output
    t = r.tool
    if t == "kpi.alarm":
        if o.get("status") != "alarm" or not o.get("ratio"):
            return None
        hours = o["run_hours"]
        text = (
            f"This investigation opened automatically: {_kpi_words(o['kpi'], o.get('filters'))} "
            f"was {o['ratio']:.1f} times its usual level for that hour of the day "
            f"({_amount(o['kpi'], o['value'])} against about {_amount(o['kpi'], o['expected'])}), "
            f"{hours} hour{'s' if hours != 1 else ''} in a row."
        )
    elif t == "kpi.compare":
        what = _kpi_words(o["kpi"], o.get("filters"))
        if o.get("insufficient") or o.get("expected") is None:
            text = f"There are no earlier days in our record to compare {what} with yet."
        else:
            text = (
                f"Over these hours {what} was {_amount(o['kpi'], o['now'])}, against about "
                f"{_amount(o['kpi'], o['expected'])} on the same hours of the previous "
                f"{o['baseline_days_used']} days ({o['pct_change']:+.0f}%)."
            )
    elif t.startswith("by."):
        dim = DIM_WORDS.get(o.get("dimension", ""), o.get("dimension", ""))
        share, seg = o.get("top_share"), o.get("top_segment")
        if o.get("insufficient") or share is None or seg is None:
            text = f"Broken down by {dim}, no single segment stands out."
        elif share > 1:
            text = (
                f"Broken down by {dim}, {seg} moved by more than the whole change "
                f"({_amount(o['kpi'], o['top_change'])}); other segments moved the other way."
            )
        else:
            text = (
                f"Broken down by {dim}, {seg} accounts for {100 * share:.0f}% of the change "
                f"({_amount(o['kpi'], o['top_change'])})."
            )
    elif t == "data.coverage":
        if o.get("complete"):
            text = "Our record covers these hours fully: the collector missed nothing."
        else:
            text = (
                f"Our record is incomplete here: the collector missed {o['gap_minutes']} "
                f"minutes and {o['empty_hours']} hours show no trades at all."
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
    if top is not None:
        claims.append(
            Claim(
                text=f"The best-supported explanation is {DRIVER_WORDS.get(top, top)}; the "
                "other possible drivers are listed in order.",
                receipt_ids=[],
                tier="inferred",
            )
        )
    return Conclusion(case_id=case_id, top_hypothesis=top, ranked=ranked, claims=claims)


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
