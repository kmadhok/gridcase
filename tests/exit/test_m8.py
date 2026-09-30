"""M8 exit: MarketCase can face the public at kanumadhok.com/marketcase.

The portfolio site forwards /marketcase/* through a Cloudflare Tunnel to the VPS, so the
service answers under that prefix as well as at / (Tailscale). Anything that arrived through the
tunnel (Cloudflare adds CF-Connecting-IP) cannot reach MCP, which can start paid investigations.
The question box stays open to the public: each visitor is counted by their real address
(the first X-Forwarded-For hop), and all visitors together spend at most $2 a day."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gridcase import config
from gridcase.ask.llm import FakeAsker
from gridcase.ask.schema import Answer
from gridcase.explain.schema import Claim
from gridcase.planner.llm import FakeLLM
from gridcase.server.app import WEB_DIST, create_app
from tests.polymarket_synth import AS_OF

TUNNEL = {"cf-connecting-ip": "203.0.113.9"}
MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2026-07-28",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


def client(store: Path, **kw: Any) -> TestClient:
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
        clock=lambda: AS_OF,
        asker_factory=lambda: asker,
        **kw,
    )
    return TestClient(app)


def test_1_the_service_answers_under_marketcase_as_well_as_at_the_root(
    market_store: Path,
) -> None:
    with client(market_store) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/marketcase/api/health").status_code == 200
        assert c.get("/marketcase/api/catalog").json()["kpis"]
        assert c.get("/marketcaseX/api/health").status_code == 404  # a prefix, not a substring


@pytest.mark.skipif(not (WEB_DIST / "index.html").exists(), reason="web UI not built")
def test_2_the_page_and_its_files_load_under_marketcase(market_store: Path) -> None:
    with client(market_store) as c:
        page = c.get("/marketcase/")
        assert page.status_code == 200 and "/marketcase/assets/" in page.text
        asset = page.text.split('src="')[1].split('"')[0]
        assert c.get(asset).status_code == 200
        assert c.get("/").status_code == 200  # the same build still works over Tailscale


def test_3_visitors_through_the_tunnel_cannot_reach_mcp(market_store: Path) -> None:
    with client(market_store) as c:
        for path in ("/mcp", "/marketcase/mcp"):
            r = c.post(path, json=MCP_INIT, headers={**MCP_HEADERS, **TUNNEL})
            assert r.status_code == 404, path
        inside = c.post("/mcp", json=MCP_INIT, headers=MCP_HEADERS)
        assert inside.status_code == 200  # over Tailscale MCP still works


def test_4_each_public_visitor_is_counted_by_their_own_address(
    market_store: Path,
) -> None:
    def ask(c: TestClient, who: str) -> int:
        headers = {**TUNNEL, "x-forwarded-for": f"{who}, 76.76.21.9"}
        body = {"page": "now", "question": "What is happening?"}
        return c.post("/marketcase/api/ask", json=body, headers=headers).status_code

    with client(market_store, ask_per_window=1) as c:
        assert [ask(c, "198.51.100.1"), ask(c, "198.51.100.1"), ask(c, "198.51.100.2")] == [
            200,
            429,
            200,
        ]


def test_5_the_question_box_spends_at_most_two_dollars_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GRIDCASE_ASK_DAILY_USD", raising=False)
    assert config.ask_daily_usd() == 2.0
