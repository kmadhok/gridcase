"""The ranker, not the model, picks the headline cause; a model that disagrees is overruled and
its pick is kept in the case trace (DECISIONS 2026-10-22)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from gridcase.explain.explainer import explain, template_conclusion
from gridcase.explain.schema import Claim, Conclusion, HypothesisVerdict
from gridcase.investigate import investigate
from gridcase.ledger.schema import Receipt
from gridcase.planner.llm import FakeLLM, LLMUsage
from gridcase.server.app import create_app
from gridcase.store.db import connect
from gridcase.trace import read_trace
from tests.polymarket_synth import AS_OF, WINDOW_START, add_trades, window_hours

RANKED = [
    HypothesisVerdict(id="D0_DATA_PROBLEM", status="supported", confidence=0.9),
    HypothesisVerdict(id="D1_ONE_MARKET", status="supported", confidence=0.8),
    HypothesisVerdict(id="D6_BROAD_BASED", status="refuted", confidence=0.1),
]


class PickingLLM(FakeLLM):
    """Writes the template conclusion, then names `pick` as the answer, as a model might."""

    def __init__(self, pick: str | None) -> None:
        super().__init__()
        self.pick = pick

    def explain(
        self,
        case_id: str,
        ranked: list[HypothesisVerdict],
        receipts: list[Receipt],
        context: dict[str, Any],
    ) -> tuple[Conclusion | None, LLMUsage]:
        self.calls += 1
        base = template_conclusion(case_id, ranked, receipts, context)
        claims = [c for c in base.claims if c.receipt_ids]
        if self.pick == "D1_ONE_MARKET":
            claims.insert(
                0,
                Claim(text="The best-supported explanation is one market.", tier="inferred"),
            )
        claims.append(Claim(text="Check the market's news next.", tier="inferred"))
        return Conclusion(
            case_id=case_id, top_hypothesis=self.pick, ranked=ranked, claims=claims
        ), LLMUsage()


def test_a_model_disagreeing_is_overruled_and_reported() -> None:
    conclusion, _, _, overruled = explain(PickingLLM("D1_ONE_MARKET"), "c1", RANKED, [], {})
    assert conclusion.top_hypothesis == "D0_DATA_PROBLEM"
    assert overruled == "D1_ONE_MARKET"
    texts = [c.text for c in conclusion.claims]
    assert texts[0].startswith("The best-supported explanation is a problem with our data")
    assert not any("is one market" in t for t in texts)
    assert "Check the market's news next." in texts  # other model claims are kept


def test_b_a_model_agreeing_or_naming_none_records_nothing() -> None:
    for pick in ("D0_DATA_PROBLEM", None):
        conclusion, _, _, overruled = explain(PickingLLM(pick), "c1", RANKED, [], {})
        assert conclusion.top_hypothesis == "D0_DATA_PROBLEM" and overruled is None
        assert not any("best-supported" in c.text for c in conclusion.claims)


def test_everything_refuted_has_no_headline_whatever_the_model_says() -> None:
    ranked = [v.model_copy(update={"status": "refuted"}) for v in RANKED]
    conclusion, _, _, overruled = explain(PickingLLM("D1_ONE_MARKET"), "c1", ranked, [], {})
    assert conclusion.top_hypothesis is None and overruled == "D1_ONE_MARKET"
    assert not any("best-supported" in c.text for c in conclusion.claims)


@pytest.fixture()
def db(market_store: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(market_store)
    yield conn
    conn.close()


def _gap_plus_spike(db: duckdb.DuckDBPyConnection) -> None:
    """M4's tripled market (test_1) and its collector gap (test_5) in the same window."""
    add_trades(
        db,
        [
            {
                "ts": h + timedelta(minutes=30 + k),
                "wallet": f"w{k}",
                "usd": 600.0,
                "condition_id": "m1",
                "trade_id": f"x{h}-{k}",
            }
            for h in window_hours()
            for k in range(4)
        ],
    )
    gap_from = WINDOW_START + timedelta(hours=2, minutes=5)
    db.execute(
        "INSERT INTO collector_gaps VALUES (?, ?, ?)",
        [gap_from, gap_from + timedelta(minutes=20), gap_from + timedelta(minutes=25)],
    )


def _explain_span(db: duckdb.DuckDBPyConnection, case_id: str) -> dict[str, Any]:
    spans = read_trace(db, case_id)["spans"]
    return next(s["detail"] for s in spans if s["name"] == "explain")


def test_c_gap_plus_spike_headline_is_the_data_problem_even_if_the_model_says_otherwise(
    db: duckdb.DuckDBPyConnection, market_store: Path
) -> None:
    _gap_plus_spike(db)
    case = investigate(db, PickingLLM("D1_ONE_MARKET"), "volume_usd", AS_OF)
    assert case.conclusion is not None
    status = {v.id: v.status for v in case.conclusion.ranked}
    assert status["D0_DATA_PROBLEM"] == "supported" and status["D1_ONE_MARKET"] == "supported"
    assert case.top_hypothesis == case.conclusion.top_hypothesis == "D0_DATA_PROBLEM"
    assert case.conclusion.claims[0].text.startswith(
        "The best-supported explanation is a problem with our data"
    )
    detail = _explain_span(db, case.case_id)
    assert detail["model_top_hypothesis"] == "D1_ONE_MARKET"
    assert detail["top_hypothesis"] == "D0_DATA_PROBLEM"

    app = create_app(db_path=market_store, token="t", llm_factory=FakeLLM, drain_interval_s=None)
    db.close()
    with TestClient(app) as client:
        spans = client.get(f"/api/cases/{case.case_id}/trace").json()["spans"]
    explain_detail = next(s["detail"] for s in spans if s["name"] == "explain")
    assert explain_detail["model_top_hypothesis"] == "D1_ONE_MARKET"


def test_b_an_agreeing_model_leaves_the_trace_as_before(db: duckdb.DuckDBPyConnection) -> None:
    _gap_plus_spike(db)
    case = investigate(db, PickingLLM("D0_DATA_PROBLEM"), "volume_usd", AS_OF)
    assert case.top_hypothesis == "D0_DATA_PROBLEM"
    detail = _explain_span(db, case.case_id)
    assert "model_top_hypothesis" not in detail and "top_hypothesis" not in detail
