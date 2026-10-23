"""The MarketCase service: MCP tools, the task registry, the follow-up drain, the JSON API."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gridcase.investigate import investigate
from gridcase.memory.cases import get_case
from gridcase.planner.llm import FakeLLM
from gridcase.server.app import create_app
from gridcase.server.scheduler import drain_scheduled
from gridcase.server.tasks import TaskRegistry
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF, WINDOW_START, add_trades, one_market_tripled


def test_task_registry_runs_work_in_the_background_and_reports_status() -> None:
    reg = TaskRegistry()
    tid = reg.submit(lambda: {"answer": 42})
    for _ in range(50):
        if reg.get(tid)["status"] == "done":
            break
        asyncio.run(asyncio.sleep(0.05))
    assert reg.get(tid)["status"] == "done" and reg.get(tid)["result"] == {"answer": 42}
    bad = reg.submit(lambda: 1 / 0)
    for _ in range(50):
        if reg.get(bad)["status"] == "failed":
            break
        asyncio.run(asyncio.sleep(0.05))
    assert "ZeroDivisionError" in reg.get(bad)["error"]
    with pytest.raises(KeyError):
        reg.get("nope")


def test_read_only_tools_return_receipts_and_cannot_write(market_store: Path) -> None:
    from mcp import Client

    app = create_app(db_path=market_store, token="t", llm_factory=FakeLLM, drain_interval_s=None)

    async def drive() -> dict[str, Any]:
        async with Client(app.state.mcp) as client:
            r = await client.call_tool(
                "tool_kpi_compare",
                {
                    "inputs": {
                        "kpi": "volume_usd",
                        "window": [
                            WINDOW_START.isoformat(),
                            (WINDOW_START + timedelta(hours=6)).isoformat(),
                        ],
                    },
                    "as_of": AS_OF.isoformat(),
                },
            )
            return dict(r.structured_content or {})

    out = asyncio.run(drive())
    assert out["tool"] == "kpi.compare" and out["receipt_id"] and out["sql"]
    c = app.state.conn.cursor()
    assert c.execute("SELECT count(*) FROM ledger").fetchone() == (0,)  # nothing persisted
    c.close()


def test_investigate_over_mcp_needs_the_token_and_returns_a_pollable_task(
    market_store: Path,
) -> None:
    from mcp import Client

    conn = connect(market_store)
    add_trades(conn, one_market_tripled())
    conn.close()
    app = create_app(
        db_path=market_store, token="secret", llm_factory=FakeLLM, drain_interval_s=None
    )

    async def drive() -> tuple[Any, dict[str, Any]]:
        async with Client(app.state.mcp) as client:
            refused = await client.call_tool(
                "investigate", {"kpi": "volume_usd", "as_of": AS_OF.isoformat(), "token": "wrong"}
            )
            r = await client.call_tool(
                "investigate", {"kpi": "volume_usd", "as_of": AS_OF.isoformat(), "token": "secret"}
            )
            tid = (r.structured_content or {})["task_id"]
            for _ in range(200):
                t = dict(
                    (await client.call_tool("get_task", {"task_id": tid})).structured_content or {}
                )
                if t["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.05)
            return refused, t

    refused, task = asyncio.run(drive())
    assert refused.is_error
    assert task["status"] == "done" and task["top_hypothesis"] == "D1_ONE_MARKET"


def test_scheduled_followup_drains_when_due(market_store: Path) -> None:
    conn = connect(market_store)
    case = investigate(conn, FakeLLM(), "volume_usd", AS_OF)
    due = conn.execute(
        "SELECT run_at FROM scheduled_tasks WHERE case_id = ?", [case.case_id]
    ).fetchone()
    assert due is not None and due[0] == AS_OF + timedelta(hours=24)
    assert drain_scheduled(conn, now=due[0] - timedelta(hours=1)) == []
    assert get_case(conn, case.case_id).state == "action_taken"
    assert drain_scheduled(conn, now=due[0] + timedelta(hours=1)) == [case.case_id]
    assert get_case(conn, case.case_id).state == "closed"
    conn.close()


def test_api_lists_cases_and_hides_nothing_sensitive(market_store: Path) -> None:
    conn = connect(market_store)
    investigate(conn, FakeLLM(), "volume_usd", AS_OF, filters={"category": "Sports"})
    conn.close()
    app = create_app(
        db_path=market_store, token="secret-token", llm_factory=FakeLLM, drain_interval_s=None
    )
    client = TestClient(app)
    cases = client.get("/api/cases").json()
    assert cases and cases[0]["entity"] == "category=Sports" and cases[0]["metric"] == "volume_usd"
    body = client.get(f"/api/cases/{cases[0]['case_id']}").text
    assert "secret-token" not in body
    assert client.get("/api/cases/nope").status_code == 404
    assert client.get("/api/map").status_code == 404  # the grid map is gone


def test_mcp_is_served_over_http_at_slash_mcp(market_store: Path) -> None:
    app = create_app(db_path=market_store, token="t", llm_factory=FakeLLM, drain_interval_s=None)
    with TestClient(app) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"accept": "application/json, text/event-stream"},
        )
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"investigate", "get_task", "get_case", "list_open_cases", "tool_by_market"} <= names
    assert "bench_summary" not in names


@pytest.mark.parametrize("directory_exists", [True, False])
def test_dbt_docs_generated_after_startup(
    market_store: Path, tmp_path: Path, directory_exists: bool
) -> None:
    docs = tmp_path / "docs"
    if directory_exists:
        docs.mkdir()
    (tmp_path / "secret.txt").write_text("outside docs")
    app = create_app(db_path=market_store, drain_interval_s=None, dbt_docs_dir=docs)
    with TestClient(app) as client:
        assert client.get("/dbt/").status_code == 404
        docs.mkdir(exist_ok=True)
        (docs / "index.html").write_text("<h1>dbt docs</h1>")
        (docs / "manifest.json").write_text('{"ready": true}')
        assert client.get("/dbt/").text == "<h1>dbt docs</h1>"
        assert client.get("/dbt/").status_code == 200
        assert client.get("/dbt/manifest.json").json() == {"ready": True}
        assert client.get("/dbt/%2e%2e/secret.txt").status_code == 404
        (docs / "escape.txt").symlink_to(tmp_path / "secret.txt")
        assert client.get("/dbt/escape.txt").status_code == 404
