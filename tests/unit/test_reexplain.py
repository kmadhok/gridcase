"""Rewrite saved conclusions using only case evidence and temporary stores."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gridcase import cli, config
from gridcase.explain.schema import Claim, Conclusion, HypothesisVerdict
from gridcase.investigate import investigate, recheck
from gridcase.ledger.schema import Receipt
from gridcase.memory.cases import get_case, save_case
from gridcase.memory.retractions import get_retraction, retract_case
from gridcase.planner.llm import FakeLLM, LLMUsage
from gridcase.store.db import connect
from gridcase.trace import read_trace
from tests.polymarket_synth import AS_OF


class RewriteLLM(FakeLLM):
    def __init__(self, fallback: bool = False) -> None:
        super().__init__()
        self.fallback = fallback
        self.context: dict[str, Any] = {}
        self.ranked: list[HypothesisVerdict] = []
        self.receipts: list[Receipt] = []

    def plan(self, *args: Any, **kwargs: Any) -> Any:
        pytest.fail("re-explain must never plan")

    def explain(
        self,
        case_id: str,
        ranked: list[HypothesisVerdict],
        receipts: list[Receipt],
        context: dict[str, Any],
    ) -> tuple[Conclusion | None, LLMUsage]:
        self.calls += 1
        self.context, self.ranked, self.receipts = context, ranked, receipts
        usage = LLMUsage(input_tokens=100, output_tokens=20, cost_usd=0.012, model="test-model")
        if self.fallback:
            return None, usage
        return Conclusion(
            case_id="model-must-not-change-id",
            top_hypothesis=None,
            ranked=[],
            claims=[
                Claim(text="Check whether this pattern continues.", tier="inferred"),
                Claim(text="Volume was 987654321 dollars.", receipt_ids=[receipts[0].receipt_id]),
            ],
            caveats=["The evidence cannot establish an outside cause."],
        ), usage


@pytest.fixture()
def saved_case(market_store: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    monkeypatch.setattr(config, "DB_PATH", market_store)
    monkeypatch.setattr(config, "has_claude_key", lambda: False)
    conn = connect(market_store)
    case = investigate(conn, FakeLLM(), "volume_usd", AS_OF, filters={"category": "Sports"})
    conn.execute("INSERT INTO learned_priors VALUES ('D1_ONE_MARKET', 'volume_usd', 'all', 2, 3)")
    retract_case(conn, case.case_id, "retain this retraction")
    conn.close()
    return market_store, case.case_id


def snapshot(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    conn = connect(path, read_only=True)
    try:
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        return {t: conn.execute(f'SELECT * FROM "{t}" ORDER BY ALL').fetchall() for t in tables}
    finally:
        conn.close()


def use_llm(monkeypatch: pytest.MonkeyPatch, llm: RewriteLLM) -> None:
    monkeypatch.setattr(cli, "choose_llm_factory", lambda mode, has_key: lambda: llm)


def test_rewrite_preserves_case_and_priors_and_appends_audit(
    saved_case: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, cid = saved_case
    conn = connect(path)
    before = get_case(conn, cid)
    old_trace = read_trace(conn, cid)["spans"]
    retraction = get_retraction(conn, cid)
    conn.close()
    stored = snapshot(path)
    llm = RewriteLLM()
    use_llm(monkeypatch, llm)
    assert cli.main(["cases", "re-explain", cid, "--llm", "fake"]) == 0
    out = capsys.readouterr().out
    assert cid in out and "kept=1 dropped=1" in out and "$0.0120" in out
    conn = connect(path)
    after = get_case(conn, cid)
    assert after.conclusion is not None and before.conclusion is not None
    assert after.conclusion.claims[0].text == "Check whether this pattern continues."
    assert after.conclusion.ranked == before.conclusion.ranked == llm.ranked
    assert after.model_dump(exclude={"conclusion", "llm_usage", "llm_calls"}) == before.model_dump(
        exclude={"conclusion", "llm_usage", "llm_calls"}
    )
    assert after.llm_usage == before.llm_usage + LLMUsage(
        input_tokens=100, output_tokens=20, cost_usd=0.012, model="test-model"
    )
    assert after.llm_calls == before.llm_calls + 1
    assert get_retraction(conn, cid) == retraction
    spans = read_trace(conn, cid)["spans"]
    assert spans[: len(old_trace)] == old_trace
    assert spans[-1]["name"] == "re-explain"
    assert spans[-1]["detail"]["model"] == "test-model" and spans[-1]["started_at"]
    assert llm.context["filters"] == {"category": "Sports"}
    assert llm.context["change"] == before.receipts[0].output
    conn.close()
    current = snapshot(path)
    for table in stored.keys() - {"cases", "case_trace"}:
        assert current[table] == stored[table]


def test_fallback_changes_nothing_and_fails(
    saved_case: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, cid = saved_case
    before = snapshot(path)
    use_llm(monkeypatch, RewriteLLM(fallback=True))
    assert cli.main(["cases", "re-explain", cid]) == 1
    out = capsys.readouterr()
    assert "template fallback" in out.out + out.err
    assert snapshot(path) == before


def test_dry_run_is_read_only_and_prints_claims_and_caveats(
    saved_case: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, cid = saved_case
    before = snapshot(path)
    raw = path.read_bytes()
    actual_connect = connect
    modes = []

    def checked_connect(path: Path, read_only: bool = False) -> Any:
        modes.append(read_only)
        return actual_connect(path, read_only=read_only)

    monkeypatch.setattr(cli, "connect", checked_connect)
    use_llm(monkeypatch, RewriteLLM())
    assert cli.main(["cases", "re-explain", cid, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Check whether this pattern continues." in out
    assert "The evidence cannot establish an outside cause." in out
    assert "within the usual range" in out
    assert modes == [True]
    assert path.read_bytes() == raw and snapshot(path) == before


def test_unknown_case_fails_without_calling_llm(
    saved_case: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path, _ = saved_case
    before = snapshot(path)
    llm = RewriteLLM()
    use_llm(monkeypatch, llm)
    assert cli.main(["cases", "re-explain", "missing"]) == 1
    out = capsys.readouterr()
    assert "unknown case missing" in out.err + out.out
    assert llm.calls == 0 and snapshot(path) == before


def test_missing_ranking_is_rebuilt_from_receipts_without_tools(
    saved_case: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, cid = saved_case
    conn = connect(path)
    case = get_case(conn, cid)
    assert case.conclusion is not None
    expected = case.conclusion.ranked
    case.conclusion.ranked = []
    save_case(conn, case)
    # Re-explaining cannot rely on current data or learned priors, even for a missing ranking.
    conn.execute("DROP TABLE trades")
    conn.execute("DROP TABLE learned_priors")
    conn.close()
    llm = RewriteLLM()
    use_llm(monkeypatch, llm)
    assert cli.main(["cases", "re-explain", cid]) == 0
    assert llm.ranked == expected


def test_closed_case_reuses_original_receipts_and_keeps_recheck_top(
    saved_case: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    path, cid = saved_case
    conn = connect(path)
    before = get_case(conn, cid)
    recheck(conn, cid, AS_OF + timedelta(hours=1))
    closed = get_case(conn, cid)
    # Recheck can legitimately choose a different headline from the original investigation.
    closed.top_hypothesis = "D6_BROAD_BASED"
    save_case(conn, closed)
    conn.close()
    llm = RewriteLLM()
    use_llm(monkeypatch, llm)
    assert cli.main(["cases", "re-explain", cid]) == 0
    conn = connect(path)
    after = get_case(conn, cid)
    conn.close()
    assert after.top_hypothesis == closed.top_hypothesis
    assert after.state == "closed" and after.recheck == closed.recheck
    assert after.verdicts == closed.verdicts
    assert llm.receipts == before.receipts


def test_step_errors_are_reapplied_and_repeat_appends_trace(
    saved_case: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, cid = saved_case
    conn = connect(path)
    case = get_case(conn, cid)
    assert case.plan is not None
    step = case.plan.steps[-1]
    case.step_errors = {step.step_id: "saved tool failure"}
    save_case(conn, case)
    conn.close()
    llm = RewriteLLM()
    use_llm(monkeypatch, llm)
    for _ in range(2):
        assert cli.main(["cases", "re-explain", cid]) == 0
    conn = connect(path)
    after = get_case(conn, cid)
    assert after.conclusion is not None
    notes = [c for c in after.conclusion.caveats if "saved tool failure" in c]
    assert len(notes) == 1 and step.tool in notes[0]
    spans = read_trace(conn, cid)["spans"]
    assert [s["name"] for s in spans[-2:]] == ["re-explain", "re-explain"]
    assert len({s["seq"] for s in spans}) == len(spans)
    assert after.llm_calls == case.llm_calls + 2
    conn.close()


def test_batch_continues_after_unknown_case_and_returns_failure(
    saved_case: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, cid = saved_case
    llm = RewriteLLM()
    use_llm(monkeypatch, llm)
    assert cli.main(["cases", "re-explain", "missing", cid]) == 1
    assert llm.calls == 1
