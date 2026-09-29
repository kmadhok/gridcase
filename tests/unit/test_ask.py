"""The question box: answers come only from a case's evidence (or read-only checks run at the
case's as_of) and pass through the same verifier as the conclusion (hard rule 6, 2026-09-29)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from anthropic import transform_schema
from fastapi.testclient import TestClient

from gridcase.ask.context import bench_context, case_context
from gridcase.ask.engine import MAX_CHECKS, answer_question, case_checker
from gridcase.ask.llm import AnthropicAsker, FakeAsker, check_tools
from gridcase.ask.schema import Answer
from gridcase.bench.inject import build_scratch
from gridcase.explain.schema import Claim
from gridcase.investigate import investigate
from gridcase.ledger.store import read_receipts
from gridcase.memory.cases import CaseRecord
from gridcase.planner.llm import FakeLLM
from gridcase.server.app import create_app
from gridcase.store.db import connect
from tests.conftest import FIXTURE_PARQUET

AS_OF = datetime(2021, 2, 16, 12)


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ask") / "s.duckdb"
    build_scratch(FIXTURE_PARQUET, path)
    conn = connect(path)
    investigate(conn, FakeLLM(), "ERCO", "forecast_error_mw", AS_OF)
    conn.close()
    return path


@pytest.fixture
def conn(store: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    c = connect(store)
    yield c
    c.close()


@pytest.fixture
def case(conn: duckdb.DuckDBPyConnection) -> CaseRecord:
    from gridcase.memory.cases import list_cases

    return list_cases(conn)[0]


def _receipt(case: CaseRecord, tool: str) -> Any:
    return next(r for r in case.receipts if r.tool == tool)


# ------------------------------------------------------------------ verification


def test_an_answer_citing_real_evidence_with_its_numbers_is_kept(case: CaseRecord) -> None:
    r = _receipt(case, "variance.plan_vs_actual")
    worst = abs(r.output["min_error_mw"])
    asker = FakeAsker(
        answer=lambda _: Answer(
            sentences=[
                Claim(
                    text=f"At the worst hour demand was {worst:,.0f} MW below forecast.",
                    receipt_ids=[r.receipt_id],
                    tier="governed",
                )
            ]
        )
    )
    res = answer_question(
        asker,
        "How big was the miss?",
        context=case_context(case),
        receipts={x.receipt_id: x for x in case.receipts},
        run_check=None,
    )
    assert [s.text for s in res.sentences] == [
        f"At the worst hour demand was {worst:,.0f} MW below forecast."
    ]
    assert res.dropped == 0


def test_a_sentence_with_a_number_not_in_its_evidence_is_dropped(case: CaseRecord) -> None:
    r = _receipt(case, "variance.plan_vs_actual")
    asker = FakeAsker(
        answer=lambda _: Answer(
            sentences=[
                Claim(text="Demand was 123,456 MW below forecast.", receipt_ids=[r.receipt_id]),
                Claim(text="Nothing in the evidence contradicts that.", receipt_ids=[]),
            ]
        )
    )
    res = answer_question(
        asker,
        "q",
        context=case_context(case),
        receipts={x.receipt_id: x for x in case.receipts},
        run_check=None,
    )
    assert res.dropped == 1
    assert [(s.text, s.tier) for s in res.sentences] == [
        ("Nothing in the evidence contradicts that.", "inferred")
    ]


def test_a_refusal_comes_back_as_a_refusal(case: CaseRecord) -> None:
    asker = FakeAsker(answer=lambda _: Answer(sentences=[], refused=True))
    res = answer_question(
        asker,
        "What's the weather in Paris?",
        context=case_context(case),
        receipts={},
        run_check=None,
    )
    assert res.refused and res.sentences == []


# ------------------------------------------------------------------ read-only checks


def test_a_check_runs_at_the_cases_as_of_and_is_not_written_to_the_case(
    conn: duckdb.DuckDBPyConnection, case: CaseRecord
) -> None:
    before = len(read_receipts(conn, case.case_id))
    check = case_checker(conn, case)
    # the model asks for a window reaching past the case's as_of; the check still cannot see it
    rec = check(
        "anomaly.detect",
        {
            "metric": "net_interchange",
            "entity": "ERCO",
            "window": {"start": "2021-02-14T12:00:00Z", "end": "2021-02-20T00:00:00Z"},
        },
    )
    assert rec.as_of == case.as_of
    assert rec.data_version is None or rec.data_version <= case.as_of
    assert len(read_receipts(conn, case.case_id)) == before


def test_new_evidence_from_a_check_can_be_cited(
    conn: duckdb.DuckDBPyConnection, case: CaseRecord
) -> None:
    def answer(ran: list[Any]) -> Answer:
        rec = ran[0]
        return Answer(
            sentences=[
                Claim(
                    text=(
                        f"Power flowing over the ties reached {rec.output['max_abs_z']:.1f} "
                        "times its normal variation."
                    ),
                    receipt_ids=[rec.receipt_id],
                    tier="governed",
                )
            ]
        )

    asker = FakeAsker(
        checks=[
            (
                "anomaly.detect",
                {
                    "metric": "net_interchange",
                    "entity": "ERCO",
                    "window": {"start": "2021-02-13T12:00:00", "end": "2021-02-16T12:00:00"},
                },
            )
        ],
        answer=answer,
    )
    res = answer_question(
        asker,
        "Were imports maxed out?",
        context=case_context(case),
        receipts={x.receipt_id: x for x in case.receipts},
        run_check=case_checker(conn, case),
    )
    assert len(res.new_receipts) == 1 and len(res.sentences) == 1
    assert res.sentences[0].receipt_ids == [res.new_receipts[0].receipt_id]


def test_checks_are_capped_per_question(conn: duckdb.DuckDBPyConnection, case: CaseRecord) -> None:
    ask = (
        "identity.balance",
        {
            "entity": "ERCO",
            "window": {"start": "2021-02-13T12:00:00", "end": "2021-02-16T12:00:00"},
        },
    )
    asker = FakeAsker(checks=[ask] * (MAX_CHECKS + 2), answer=lambda ran: Answer(sentences=[]))
    res = answer_question(
        asker, "q", context=case_context(case), receipts={}, run_check=case_checker(conn, case)
    )
    assert asker.checks_attempted == MAX_CHECKS + 2
    assert len(res.new_receipts) == MAX_CHECKS


def test_a_failing_check_is_reported_to_the_model_not_raised(
    conn: duckdb.DuckDBPyConnection, case: CaseRecord
) -> None:
    bad = (
        "anomaly.detect",
        {
            "metric": "demand",
            "entity": "ERCO",
            "window": {"start": "2015-01-01T00:00:00", "end": "2015-01-02T00:00:00"},
        },
    )
    asker = FakeAsker(checks=[bad], answer=lambda ran: Answer(sentences=[]))
    res = answer_question(
        asker, "q", context=case_context(case), receipts={}, run_check=case_checker(conn, case)
    )
    assert res.new_receipts == [] and len(asker.check_errors) == 1


# ------------------------------------------------------------------ context and the real asker


def test_case_context_carries_causes_falsifiers_evidence_and_glossary(case: CaseRecord) -> None:
    ctx = case_context(case)
    assert {c["id"] for c in ctx["causes"]} == {v.id for v in case.conclusion.ranked}  # type: ignore[union-attr]
    assert all(c["name"] and c["what_rules_it_out"] for c in ctx["causes"])
    assert {e["cite_as"] for e in ctx["evidence"]} == {r.receipt_id[:8] for r in case.receipts}
    assert all("sql" not in e for e in ctx["evidence"])  # queries stay in the UI, not the prompt
    assert "usual gap" in ctx["glossary"] and "ruled out" in ctx["glossary"]


def test_bench_context_turns_results_into_citable_evidence() -> None:
    runs = [
        {
            "name": "r1",
            "config": {"model": "fake", "validator": True},
            "summary": {
                "synthetic": {"n": 200, "top1": 0.48, "top3": 0.73},
                "replay": {"n": 5, "top1": 0.6},
                "null": {"n": 100, "false_alarm_rate": 0.24},
            },
            "replay": [],
        }
    ]
    ctx, receipts = bench_context(runs)
    asker = FakeAsker(
        answer=lambda _: Answer(
            sentences=[
                Claim(
                    text="It named the right cause for 60% of the 5 real events.",
                    receipt_ids=[next(iter(receipts))],
                    tier="governed",
                )
            ]
        )
    )
    res = answer_question(
        asker, "How accurate is it?", context=ctx, receipts=receipts, run_check=None
    )
    assert len(res.sentences) == 1


def test_every_check_tool_has_a_strict_schema_with_real_properties() -> None:
    tools = check_tools()
    assert len(tools) == 10
    for t in tools:
        assert t["strict"] is True and t["name"].startswith("check_")
        props = t["input_schema"]["properties"]
        assert len(props) >= 2
    answer_schema = transform_schema(Answer.model_json_schema())
    assert "sentences" in answer_schema["properties"]
    assert AnthropicAsker.max_checks == MAX_CHECKS


# ------------------------------------------------------------------ API


def _client(store: Path, **kw: Any) -> TestClient:
    asker = FakeAsker(
        answer=lambda _: Answer(
            sentences=[Claim(text="The evidence does not say.", receipt_ids=[])]
        )
    )
    app = create_app(
        db_path=store,
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        asker_factory=lambda: asker,
        **kw,
    )
    return TestClient(app)


def test_api_answers_a_question_about_a_case(store: Path, case: CaseRecord) -> None:
    with _client(store) as client:
        r = client.post("/api/ask", json={"case_id": case.case_id, "question": "Why not weather?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sentences"][0]["tier"] == "inferred" and body["refused"] is False
    assert body["new_receipts"] == [] and "cost_usd" in body


def test_api_rejects_unknown_cases_and_overlong_questions(store: Path) -> None:
    with _client(store) as client:
        assert client.post("/api/ask", json={"case_id": "nope", "question": "q"}).status_code == 404
        assert (
            client.post("/api/ask", json={"page": "bench", "question": "x" * 501}).status_code
            == 422
        )
        assert client.post("/api/ask", json={"question": "no target"}).status_code == 422


def test_api_rate_limits_each_visitor(store: Path, case: CaseRecord) -> None:
    with _client(store, ask_per_window=2) as client:
        codes = [
            client.post("/api/ask", json={"case_id": case.case_id, "question": "q"}).status_code
            for _ in range(3)
        ]
    assert codes == [200, 200, 429]


def test_api_stops_when_the_daily_budget_is_spent(store: Path, case: CaseRecord) -> None:
    with _client(store, ask_daily_usd=0.0) as client:
        r = client.post("/api/ask", json={"case_id": case.case_id, "question": "q"})
    assert r.status_code == 429 and "budget" in r.json()["detail"]


def test_an_uncited_sentence_that_states_a_number_is_dropped(case: CaseRecord) -> None:
    """Interpretation may explain; it may not state figures. Numbers need their evidence."""
    asker = FakeAsker(
        answer=lambda _: Answer(
            sentences=[
                Claim(text="Demand came in 28,885 MW below plan.", receipt_ids=[], tier="inferred"),
                Claim(text="That is a very large miss for this grid.", receipt_ids=[]),
            ]
        )
    )
    res = answer_question(asker, "q", context=case_context(case), receipts={}, run_check=None)
    assert res.dropped == 1
    assert [s.text for s in res.sentences] == ["That is a very large miss for this grid."]


def test_short_evidence_codes_resolve_to_full_receipt_ids(case: CaseRecord) -> None:
    """The model cites 8-character codes (easier to copy than 24-character ids); the engine
    maps them back before verification, and the context offers only the short codes."""
    r = _receipt(case, "variance.plan_vs_actual")
    ctx = case_context(case)
    assert all(len(e["cite_as"]) == 8 and "receipt_id" not in e for e in ctx["evidence"])
    assert all("cites" in s for s in ctx["conclusion_sentences"])
    worst = abs(r.output["min_error_mw"])
    asker = FakeAsker(
        answer=lambda _: Answer(
            sentences=[
                Claim(
                    text=f"Demand came in {worst:,.0f} MW below plan.",
                    receipt_ids=[r.receipt_id[:8]],
                    tier="governed",
                )
            ]
        )
    )
    res = answer_question(
        asker,
        "q",
        context=ctx,
        receipts={x.receipt_id: x for x in case.receipts},
        run_check=None,
    )
    assert res.dropped == 0 and res.sentences[0].receipt_ids == [r.receipt_id]
