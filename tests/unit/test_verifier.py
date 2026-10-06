"""The verifier: no receipt, no governed claim; numbers must appear in the cited receipts."""

from __future__ import annotations

from datetime import datetime

from gridcase.explain.schema import Claim, Conclusion, HypothesisVerdict
from gridcase.explain.verifier import numbers_in, verify
from gridcase.ledger.schema import Receipt


def receipt(rid: str, output: dict[str, object]) -> Receipt:
    r = Receipt.build(
        tool="bridge.fuel",
        inputs={"entity": "ERCO"},
        as_of=datetime(2021, 2, 16, 12),
        output=output,
        data_version=None,
        effect_size=None,
        runtime_ms=1,
        sql=[],
    )
    return r.model_copy(update={"receipt_id": rid})


R1 = receipt(
    "r1",
    {
        "total_change_mw": -16354.0,
        "top_fuel": "natural_gas",
        "top_share": 0.5036,
        "fuels": [{"fuel": "natural_gas", "delta_mw": -8236.0}],
    },
)
R2 = receipt("r2", {"max_abs_z": 12.3, "n_missing": 0})
RECEIPTS = {"r1": R1, "r2": R2}


def conclusion(*claims: Claim) -> Conclusion:
    return Conclusion(
        case_id="c",
        top_hypothesis="H2_SUPPLY_SHORTFALL",
        ranked=[HypothesisVerdict(id="H2_SUPPLY_SHORTFALL", status="open", confidence=0.7)],
        claims=list(claims),
        caveats=[],
    )


def test_numbers_are_extracted_with_thousands_separators_and_percents() -> None:
    assert numbers_in("Gas fell 8,236 MW (50.4% of a 16,354 MW drop); z was 12.3") == [
        8236.0,
        50.4,
        16354.0,
        12.3,
    ]


def test_claim_with_matching_numbers_survives() -> None:
    c = Claim(
        text="Natural gas fell 8,236 MW, 50% of the 16,354 MW drop.",
        receipt_ids=["r1"],
        tier="governed",
    )
    out, dropped = verify(conclusion(c), RECEIPTS)
    assert [x.text for x in out.claims] == [c.text] and dropped == []


def test_claim_whose_number_is_not_in_its_receipts_is_dropped() -> None:
    c = Claim(text="Natural gas fell 12,000 MW.", receipt_ids=["r1"], tier="governed")
    out, dropped = verify(conclusion(c), RECEIPTS)
    assert out.claims == [] and "12000" in dropped[0]


def test_one_percent_tolerance_and_share_as_percent() -> None:
    ok = Claim(
        text="Gas was 50.4% of the drop, about 8,300 MW.", receipt_ids=["r1"], tier="derived"
    )
    out, _ = verify(conclusion(ok), RECEIPTS)
    assert len(out.claims) == 1


def test_claim_citing_missing_receipt_is_dropped() -> None:
    c = Claim(text="Something happened.", receipt_ids=["nope"], tier="governed")
    out, dropped = verify(conclusion(c), RECEIPTS)
    assert out.claims == [] and "nope" in dropped[0]


def test_uncited_claim_is_kept_but_forced_to_inferred() -> None:
    c = Claim(text="Operators likely shed load.", receipt_ids=[], tier="governed")
    out, dropped = verify(conclusion(c), RECEIPTS)
    assert out.claims[0].tier == "inferred" and dropped == []


def test_claim_without_numbers_only_needs_its_receipts_to_exist() -> None:
    c = Claim(text="Natural gas was the largest contributor.", receipt_ids=["r1"], tier="governed")
    out, _ = verify(conclusion(c), RECEIPTS)
    assert len(out.claims) == 1


def test_the_alarm_that_opened_a_case_gets_a_plain_verified_sentence() -> None:
    from datetime import datetime

    from gridcase.explain.explainer import _claim_for
    from gridcase.explain.schema import Conclusion
    from gridcase.explain.verifier import verify
    from gridcase.ledger.schema import Receipt

    r = Receipt.build(
        tool="kpi.alarm",
        inputs={"kpi": "volume_usd"},
        as_of=datetime(2026, 10, 5, 16),
        output={
            "kpi": "volume_usd",
            "filters": {"category": "Sports"},
            "status": "alarm",
            "direction": "up",
            "run_hours": 2,
            "value": 1_250_000.0,
            "expected": 400_000.0,
            "ratio": 3.125,
            "z": 22.8,
        },
        data_version=None,
        effect_size=22.8,
        runtime_ms=1,
        sql=[],
    )
    claim = _claim_for(r)
    assert claim is not None and "opened automatically" in claim.text and "Sports" in claim.text
    kept, dropped = verify(
        Conclusion(case_id="c", top_hypothesis=None, ranked=[], claims=[claim]), {r.receipt_id: r}
    )
    assert dropped == [] and len(kept.claims) == 1


def test_numbers_inside_labels_copied_from_the_evidence_are_not_claims() -> None:
    """A segment called '$100-1k' or a market called 'Ethereum above $5k' is quoted, not
    asserted: only the sentence's own figures must match the evidence."""
    from datetime import datetime

    from gridcase.explain.schema import Claim, Conclusion
    from gridcase.explain.verifier import verify
    from gridcase.ledger.schema import Receipt

    r = Receipt.build(
        tool="by.size",
        inputs={},
        as_of=datetime(2026, 10, 1),
        output={"top_segment": "$100-1k", "top_share": 0.72, "top_change": 13412.57},
        data_version=None,
        effect_size=0.72,
        runtime_ms=1,
        sql=[],
    )
    ok = Claim(
        text="Trades of $100-1k account for 72% of the change ($13,413).",
        receipt_ids=[r.receipt_id],
    )
    bad = Claim(text="Trades of $100-1k account for 95% of the change.", receipt_ids=[r.receipt_id])
    kept, dropped = verify(
        Conclusion(case_id="c", top_hypothesis=None, ranked=[], claims=[ok, bad]), {r.receipt_id: r}
    )
    assert [c.text for c in kept.claims] == [ok.text] and len(dropped) == 1


def test_abbreviated_amounts_like_2_9m_match_their_evidence() -> None:
    from gridcase.explain.verifier import numbers_in

    assert numbers_in("Sports traded $2.9M, crypto $540k and politics $1.2 million.") == [
        2_900_000.0,
        540_000.0,
        1_200_000.0,
    ]
    assert numbers_in("7,451 traders, 12 bn") == [7451.0, 12_000_000_000.0]


def test_dates_written_with_month_names_and_am_pm_times_are_not_claims() -> None:
    assert numbers_in("On Oct 3 at 7pm, and on October 4th at 11 am, volume rose") == []
    assert numbers_in("From 3 October to Sep. 29, 2026") == []
    assert numbers_in("Oct 3 saw 412 trades") == [412.0]  # the count is still a claim


def test_a_period_is_not_a_claim_only_when_the_evidence_covers_it() -> None:
    week = Receipt.build(
        tool="kpi.series",
        inputs={},
        as_of=datetime(2026, 10, 5, 18),
        output={"window": {"span": ["7 days", "168 hours"]}, "rows": [{"value": 450.0}]},
        data_version=None,
        effect_size=None,
        runtime_ms=0,
        sql=[],
    )
    ok = Claim(
        text="Over the last 7 days, Sports traded $450.",
        receipt_ids=[week.receipt_id],
        tier="governed",
    )
    wrong = Claim(
        text="Over the last 3 days, Sports traded $450.",
        receipt_ids=[week.receipt_id],
        tier="governed",
    )
    invented = Claim(text="Sports had 7 trades.", receipt_ids=[week.receipt_id], tier="governed")
    out, dropped = verify(
        Conclusion(case_id="x", top_hypothesis=None, ranked=[], claims=[ok, wrong, invented]),
        {week.receipt_id: week},
    )
    assert [c.text for c in out.claims] == [ok.text] and len(dropped) == 2


def test_clock_times_with_seconds_are_not_claims() -> None:
    assert numbers_in("The busiest hour was 2026-10-04 at 20:00:00, with $8.45M") == [8.45e6]
