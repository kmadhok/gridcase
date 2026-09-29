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
        inputs={"entity": "ERCO"},
        as_of=datetime(2021, 2, 16, 12),
        output={
            "entity": "ERCO",
            "alarm": True,
            "direction": "below",
            "run_hours": 30,
            "worst_value": -39.5,
            "mean": 0.4,
            "std": 2.1,
            "k_sigma": 3.0,
            "lookback_days": 28,
        },
        data_version=None,
        effect_size=-18.6,
        runtime_ms=1,
        sql=[],
    )
    claim = _claim_for(r)
    assert claim is not None and "opened automatically" in claim.text
    kept, dropped = verify(
        Conclusion(case_id="c", top_hypothesis=None, ranked=[], claims=[claim]), {r.receipt_id: r}
    )
    assert dropped == [] and len(kept.claims) == 1
