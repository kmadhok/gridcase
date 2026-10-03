"""M11 exit: persisted investigation timings, costs, failures and public trace API."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gridcase.explain.schema import Claim, Conclusion, HypothesisVerdict
from gridcase.investigate import investigate, recheck
from gridcase.ledger.schema import Receipt
from gridcase.memory.cases import list_cases, new_case
from gridcase.planner.context import PlanContext
from gridcase.planner.llm import FakeLLM, LLMUsage
from gridcase.planner.schema import InvestigationPlan
from gridcase.server.app import create_app
from gridcase.store.db import connect
from gridcase.store.schema import create_schema
from gridcase.tools.base import TOOLS, ToolError, run_tool
from gridcase.trace import active_tracer, read_trace
from tests.polymarket_synth import AS_OF


class MeteredFake(FakeLLM):
    def plan(
        self, ctx: PlanContext, feedback: list[str]
    ) -> tuple[InvestigationPlan | None, LLMUsage]:
        plan, _ = super().plan(ctx, feedback)
        return (None if self.calls == 1 else plan), LLMUsage(
            input_tokens=100, output_tokens=20, cost_usd=0.012345, model="test-model"
        )

    def explain(
        self,
        case_id: str,
        ranked: list[HypothesisVerdict],
        receipts: list[Receipt],
        context: dict[str, Any],
    ) -> tuple[Conclusion | None, LLMUsage]:
        self.calls += 1
        return Conclusion(
            case_id=case_id,
            top_hypothesis=None,
            ranked=ranked,
            claims=[
                Claim(text="An interpretation.", receipt_ids=[]),
                Claim(text="Unsupported model output.", receipt_ids=["missing"]),
            ],
        ), LLMUsage(input_tokens=300, output_tokens=40, cost_usd=0.023456, model="test-model")


def test_1_trace_records_order_parentage_failures_cost_and_counts(
    market_store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect(market_store)
    # Load the tools before planting a failure in an actual planned check.
    from gridcase.tools.base import _ensure_loaded

    _ensure_loaded()

    def fail(*args: Any) -> Any:
        raise ToolError("planted check failure")

    monkeypatch.setitem(TOOLS, "by.market", fail)
    case = investigate(conn, MeteredFake(), "volume_usd", AS_OF)
    trace = read_trace(conn, case.case_id)
    assert trace["recorded"] is True
    spans = trace["spans"]
    top = [s for s in spans if s["parent_seq"] is None]
    assert [s["name"] for s in top] == [
        "compare",
        "plan",
        "execute",
        "explain",
        "verify",
        "publish",
    ]
    assert [s["kind"] for s in top] == ["stage", "llm", "stage", "llm", "stage", "stage"]
    by_seq = {s["seq"]: s for s in spans}
    for s in spans:
        assert s["offset_ms"] >= 0 and s["ms"] >= 0
        if s["parent_seq"] is not None:
            p = by_seq[s["parent_seq"]]
            assert s["offset_ms"] >= p["offset_ms"]
            assert s["offset_ms"] + s["ms"] <= p["offset_ms"] + p["ms"] + 0.01
    checks = [s for s in spans if s["parent_seq"] == top[2]["seq"]]
    assert case.plan is not None
    assert len(checks) == len(case.plan.steps)
    assert [s["detail"]["step_id"] for s in checks] == [s.step_id for s in case.plan.steps]
    failed = [s for s in checks if s["status"] == "error"]
    assert len(failed) == 1 and failed[0]["detail"]["error"] == "planted check failure"
    receipts = {r.receipt_id: r for r in case.receipts}
    for s in checks:
        assert "inputs" in s["detail"]
        if s["status"] == "ok":
            assert receipts[s["detail"]["receipt_id"]].tool == s["name"]
    assert top[1]["detail"]["attempts"] == 2
    assert top[4]["detail"] == {"kept": 1, "dropped": 1, "interpretation": 1}
    totals = trace["totals"]
    assert totals["model_calls"] == 3
    assert totals["input_tokens"] == 500 and totals["output_tokens"] == 80
    assert totals["cost_usd"] == case.llm_usage.cost_usd
    assert totals["checks_run"] == len(checks) and totals["checks_failed"] == 1
    assert totals["wall_ms"] >= max(s["offset_ms"] + s["ms"] for s in spans)
    assert "Unsupported model output" not in json.dumps(trace)
    assert "An interpretation." not in json.dumps(trace)
    assert active_tracer.get() is None
    # Neither a standalone check nor the later deterministic re-check adds spans.
    before = conn.execute("SELECT count(*) FROM case_trace").fetchone()
    r = case.receipts[0]
    run_tool(conn, r.tool, r.inputs, AS_OF)
    recheck(conn, case.case_id, AS_OF)
    assert conn.execute("SELECT count(*) FROM case_trace").fetchone() == before
    conn.close()


def test_2_explain_failure_still_saves_earlier_spans_and_restores_context(
    market_store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("planted explanation failure")

    monkeypatch.setattr(importlib.import_module("gridcase.investigate"), "explain", fail)
    conn = connect(market_store)
    with pytest.raises(RuntimeError, match="planted explanation failure"):
        investigate(conn, FakeLLM(), "volume_usd", AS_OF)
    case = list_cases(conn)[0]
    trace = read_trace(conn, case.case_id)
    top = [s for s in trace["spans"] if s["parent_seq"] is None]
    assert [s["name"] for s in top] == ["compare", "plan", "execute", "explain"]
    assert top[-1]["status"] == "error"
    assert top[-1]["detail"]["error"] == "planted explanation failure"
    assert active_tracer.get() is None
    conn.close()


def test_3_api_old_cases_unknown_cases_and_idempotent_schema(market_store: Path) -> None:
    conn = connect(market_store)
    old = new_case(conn, "all", "volume_usd", AS_OF)
    r = run_tool(
        conn,
        "kpi.compare",
        {
            "kpi": "volume_usd",
            "window": ["2026-10-05T12:00:00", "2026-10-05T14:00:00"],
            "baseline_days": 7,
        },
        AS_OF,
        case_id=old.case_id,
    )
    assert r.tool == "kpi.compare"
    case = investigate(conn, FakeLLM(), "volume_usd", AS_OF)
    create_schema(conn)
    create_schema(conn)
    expected = read_trace(conn, case.case_id)
    conn.close()
    app = create_app(db_path=market_store, token="t", llm_factory=FakeLLM, drain_interval_s=None)
    with TestClient(app) as client:
        response = client.get(f"/api/cases/{case.case_id}/trace")
        assert response.status_code == 200 and response.json() == expected
        assert (
            response.json()["totals"]["cost_usd"]
            == client.get(f"/api/cases/{case.case_id}").json()["cost_usd"]
        )
        empty = client.get(f"/api/cases/{old.case_id}/trace").json()
        assert empty["recorded"] is False and empty["spans"] == []
        assert client.get("/api/cases/no-such-case/trace").status_code == 404
