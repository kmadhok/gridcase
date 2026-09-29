"""Phase 6 exit test: the system is drivable from any MCP client and readable in a browser.

Offline: an MCP client (the Python SDK, in-process) drives `investigate` on a synthetic case
and polls the task to completion; the read-only API serves the Uri case with every claim's
receipts and their SQL; the UI bundle exists and its routes reach a conclusion in <= 3
clicks (asserted on the built route table and by a Playwright walk when Playwright is
installed). Lighthouse is `live`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gridcase.bench.inject import build_scratch, inject, sample_scenario
from gridcase.investigate import investigate
from gridcase.planner.llm import FakeLLM
from gridcase.server.app import create_app
from gridcase.store.db import connect
from tests.conftest import FIXTURE_PARQUET

ROOT = Path(__file__).resolve().parents[2]
WEB_DIST = ROOT / "web" / "dist"
URI_AS_OF = datetime(2021, 2, 16, 12)


@pytest.fixture(scope="module")
def store_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("srv") / "store.duckdb"
    build_scratch(FIXTURE_PARQUET, path)
    conn = connect(path)
    investigate(conn, FakeLLM(), entity="ERCO", metric="forecast_error_mw", as_of=URI_AS_OF)
    conn.close()
    return path


@pytest.fixture(scope="module")
def app(store_path: Path):  # type: ignore[no-untyped-def]
    return create_app(db_path=store_path, token="t0k3n", llm_factory=FakeLLM, drain_interval_s=None)


def test_1_mcp_client_investigates_and_polls_the_task(store_path: Path, app) -> None:  # type: ignore[no-untyped-def]
    from mcp import Client

    conn = connect(store_path)
    sc = sample_scenario("H2_SUPPLY_SHORTFALL", seed=3, entity="ERCO")
    inject(conn, sc)
    conn.close()

    async def drive() -> dict[str, object]:
        async with Client(app.state.mcp) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert {
                "investigate",
                "get_task",
                "get_case",
                "list_open_cases",
                "bench_summary",
            } <= tools
            assert "tool_identity_balance" in tools and not any("ontology" in t for t in tools)
            started = await client.call_tool(
                "investigate",
                {
                    "entity": "ERCO",
                    "metric": sc.metric,
                    "as_of": sc.as_of.isoformat(),
                    "token": "t0k3n",
                },
            )
            task_id = started.structured_content["task_id"]
            for _ in range(600):
                polled = (
                    await client.call_tool("get_task", {"task_id": task_id})
                ).structured_content
                if polled["status"] in ("done", "failed"):
                    return dict(polled)
                await asyncio.sleep(0.2)
            raise AssertionError("task did not finish")

    result = asyncio.run(drive())
    assert result["status"] == "done", result
    assert result["case_id"] and result["top_hypothesis"]


def test_1b_investigate_requires_the_token(app) -> None:  # type: ignore[no-untyped-def]
    from mcp import Client

    async def drive() -> Any:
        async with Client(app.state.mcp) as client:
            r = await client.call_tool(
                "investigate",
                {
                    "entity": "ERCO",
                    "metric": "forecast_error_mw",
                    "as_of": "2021-02-16T12:00:00",
                    "token": "nope",
                },
            )
            return r

    r = asyncio.run(drive())
    assert r.is_error or (r.structured_content or {}).get("error")


def test_2_case_api_serves_every_claim_with_receipts_and_sql(app) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    cases = client.get("/api/cases").json()
    uri = next(c for c in cases if c["entity"] == "ERCO" and c["as_of"].startswith("2021-02-16"))
    case = client.get(f"/api/cases/{uri['case_id']}").json()
    assert case["conclusion"]["top_hypothesis"] == "H2_SUPPLY_SHORTFALL"
    receipts = {r["receipt_id"]: r for r in case["receipts"]}
    cited = [c for c in case["conclusion"]["claims"] if c["tier"] != "inferred"]
    assert cited and all(c["receipt_ids"] for c in cited)
    for c in cited:
        for rid in c["receipt_ids"]:
            assert (
                rid in receipts and receipts[rid]["sql"] and receipts[rid]["sql"][0]["sql"].strip()
            )
    assert client.get("/api/cases/nope").status_code == 404


def test_2b_map_and_bench_endpoints(app) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    m = client.get("/api/map").json()
    assert {r["entity"] for r in m["regions"]} >= {"ERCO"}
    erco = next(r for r in m["regions"] if r["entity"] == "ERCO")
    assert "forecast_error_pct" in erco and "case_id" in erco
    b = client.get("/api/bench").json()
    assert "runs" in b and (not b["runs"] or "synthetic" in b["runs"][-1]["summary"])
    assert client.get("/api/health").json()["ok"] is True


def test_3_ui_bundle_exists_and_reaches_the_uri_conclusion_in_three_clicks(app) -> None:  # type: ignore[no-untyped-def]
    index = WEB_DIST / "index.html"
    if not index.exists():
        pytest.skip("web/dist not built (cd web && npm run build)")
    client = TestClient(app)
    html = client.get("/").text
    assert '<div id="root"' in html
    # Route table: landing -> region tile/map click -> case -> conclusion is on the case page
    routes = json.loads((ROOT / "web" / "src" / "routes.json").read_text())
    assert routes["landing"] == "/" and routes["case"].startswith("/case/")
    assert routes["clicks_to_conclusion"] <= 3


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GRIDCASE_LIVE_UI"), reason="needs Playwright + a built UI")
def test_4_playwright_walk_and_lighthouse(store_path: Path) -> None:
    pw = pytest.importorskip("playwright.sync_api")
    import threading

    import uvicorn

    app = create_app(db_path=store_path, token="t", llm_factory=FakeLLM, drain_interval_s=None)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    time.sleep(1.5)
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page()
            page.goto("http://127.0.0.1:8765/")
            page.get_by_test_id("region-ERCO").click()  # 1
            page.get_by_test_id("open-case").click()  # 2
            page.get_by_test_id("claim-0").click()  # 3: expand a claim to its receipt + SQL
            assert page.get_by_test_id("receipt-sql").first.is_visible()
            b.close()
        if shutil.which("lighthouse"):
            out = subprocess.run(
                [
                    "lighthouse",
                    "http://127.0.0.1:8765/",
                    "--quiet",
                    "--chrome-flags=--headless",
                    "--only-categories=performance",
                    "--throttling-method=simulate",
                    "--output=json",
                    "--output-path=stdout",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            score = json.loads(out.stdout)["categories"]["performance"]["score"]
            assert score >= 0.8, score
    finally:
        server.should_exit = True
