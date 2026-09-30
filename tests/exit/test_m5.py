"""M5 exit: the Now page and the question box on live data.

The Now page shows every watched KPI's last 48 hours against its usual band with a status, how
fresh our data is, and the latest investigations. From it a PM can ask a question; the answer
uses only live evidence (the KPIs compared with their usual level, the category breakdown, and
up to three read-only checks at the current time) and passes the verifier."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gridcase.ask.llm import FakeAsker
from gridcase.ask.schema import Answer
from gridcase.explain.schema import Claim
from gridcase.investigate import investigate
from gridcase.ledger.schema import Receipt
from gridcase.market_alarm import load_watch
from gridcase.planner.llm import FakeLLM
from gridcase.server.app import create_app
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF, WINDOW_START, add_trades, one_market_tripled

WEB = Path(__file__).resolve().parents[2] / "web"


def _app(store: Path, asker: Any = None) -> Any:
    return create_app(
        db_path=store,
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        asker_factory=(lambda: asker) if asker else None,
        clock=lambda: AS_OF,
    )


def test_1_now_shows_every_watched_kpi_against_its_usual_band(market_store: Path) -> None:
    conn = connect(market_store)
    add_trades(conn, one_market_tripled())
    investigate(conn, FakeLLM(), "volume_usd", AS_OF, filters={"category": "Sports"})
    conn.close()
    with TestClient(_app(market_store)) as client:
        now = client.get("/api/now").json()
    watched, _ = load_watch()
    assert len(now["series"]) == len(watched) == 18
    first = now["series"][0]
    assert first["kpi"] == "volume_usd" and first["segment"] == "all" and first["label"]
    assert len(first["points"]) == 48 and {"t", "value", "usual", "low", "high"} <= set(
        first["points"][0]
    )
    assert first["status"] in {"alarm", "normal", "learning"}
    sports = next(s for s in now["series"] if s["segment"] == "category=Sports")
    assert sports["status"] == "alarm" and sports["direction"] == "up"
    assert now["freshness"]["newest_trade"] and now["freshness"]["minutes_behind"] is not None
    assert now["cases"][0]["segment"] == "category=Sports"


def test_2_a_question_from_the_now_page_is_answered_from_live_evidence(market_store: Path) -> None:
    conn = connect(market_store)
    add_trades(conn, one_market_tripled())
    conn.close()

    def answer(ran: list[Receipt]) -> Answer:
        r = ran[0]
        return Answer(
            sentences=[
                Claim(
                    text=(
                        f"In Sports, {r.output['top_segment']} moved by "
                        f"${r.output['top_change']:,.0f}."
                    ),
                    receipt_ids=[r.receipt_id],
                    tier="governed",
                )
            ]
        )

    window = {"start": WINDOW_START.isoformat(), "end": AS_OF.replace(minute=0).isoformat()}
    asker = FakeAsker(
        checks=[
            (
                "by.market",
                {
                    "kpi": "volume_usd",
                    "filters": [{"dimension": "category", "value": "Sports"}],
                    "window": window,
                },
            )
        ],
        answer=answer,
    )
    with TestClient(_app(market_store, asker)) as client:
        r = client.post("/api/ask", json={"page": "now", "question": "Why is sports volume up?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dropped"] == 0 and len(body["sentences"]) == 1
    assert "England vs Czechia" in body["sentences"][0]["text"]
    assert body["new_receipts"][0]["as_of"].startswith(AS_OF.isoformat()[:16])


def test_3_the_now_context_offers_citable_evidence_for_every_team_kpi(market_store: Path) -> None:
    from gridcase.ask.context import now_context

    conn = connect(market_store)
    ctx, receipts = now_context(conn, AS_OF)
    conn.close()
    tools = sorted(r.tool for r in receipts.values())
    assert tools.count("kpi.compare") == 10 and "by.category" in tools
    assert {s["series"] for s in ctx["series"]} >= {"volume_usd", "active_traders"}
    assert all("cite_as" in e for e in ctx["evidence"])


def test_4_questions_need_a_case_or_the_now_page(market_store: Path) -> None:
    with TestClient(_app(market_store)) as client:
        assert client.post("/api/ask", json={"question": "hi"}).status_code == 422
        assert client.post("/api/ask", json={"page": "bench", "question": "hi"}).status_code == 422


@pytest.mark.skipif(not (WEB / "dist" / "index.html").exists(), reason="UI not built")
def test_5_the_ui_reaches_an_investigation_in_two_clicks() -> None:
    routes = json.loads((WEB / "src" / "routes.json").read_text())
    assert routes["landing"] == "/" and routes["clicks_to_conclusion"] <= 2
    assert "bench" not in routes and "map" not in json.dumps(routes).lower()
