"""Server internals: task registry, read-only tool exposure, scheduled re-check drain."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gridcase.bench.inject import build_scratch
from gridcase.investigate import investigate
from gridcase.memory.cases import get_case
from gridcase.planner.llm import FakeLLM
from gridcase.server.app import create_app
from gridcase.server.scheduler import drain_scheduled
from gridcase.server.tasks import TaskRegistry
from gridcase.store.db import connect
from tests.conftest import FIXTURE_PARQUET

URI_AS_OF = datetime(2021, 2, 16, 12)


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "s.duckdb"
    build_scratch(FIXTURE_PARQUET, path)
    return path


def test_task_registry_runs_work_in_the_background_and_reports_status() -> None:
    reg = TaskRegistry()
    tid = reg.submit(lambda: {"answer": 42})
    for _ in range(50):
        t = reg.get(tid)
        if t["status"] == "done":
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


def test_read_only_tools_return_receipts_and_cannot_write(store: Path) -> None:
    from mcp import Client

    app = create_app(db_path=store, token="t", llm_factory=FakeLLM, drain_interval_s=None)

    async def drive() -> dict[str, object]:
        async with Client(app.state.mcp) as client:
            r = await client.call_tool(
                "tool_identity_balance",
                {
                    "inputs": {
                        "entity": "ERCO",
                        "window": ["2023-05-01T00:00:00", "2023-05-04T00:00:00"],
                    },
                    "as_of": "2023-05-10T00:00:00",
                },
            )
            return dict(r.structured_content or {})

    out = asyncio.run(drive())
    assert out["tool"] == "identity.balance" and out["receipt_id"] and out["sql"]
    c = app.state.conn.cursor()
    assert c.execute("SELECT count(*) FROM ledger").fetchone() == (0,)  # nothing persisted
    c.close()


def test_scheduled_recheck_drains_when_due(store: Path) -> None:
    conn = connect(store)
    case = investigate(conn, FakeLLM(), entity="ERCO", metric="forecast_error_mw", as_of=URI_AS_OF)
    due = conn.execute(
        "SELECT run_at FROM scheduled_tasks WHERE case_id = ?", [case.case_id]
    ).fetchone()
    assert due is not None
    # not yet due: nothing happens
    assert drain_scheduled(conn, now=due[0] - timedelta(hours=1)) == []
    assert get_case(conn, case.case_id).state == "action_taken"
    # due: the re-check runs at the scheduled time and the case closes
    done = drain_scheduled(conn, now=due[0] + timedelta(hours=1))
    assert done == [case.case_id]
    assert get_case(conn, case.case_id).state == "closed"
    assert conn.execute(
        "SELECT status FROM scheduled_tasks WHERE case_id = ?", [case.case_id]
    ).fetchone() == ("done",)
    conn.close()


def test_api_lists_cases_newest_first_and_hides_nothing_sensitive(store: Path) -> None:
    conn = connect(store)
    investigate(conn, FakeLLM(), entity="ERCO", metric="forecast_error_mw", as_of=URI_AS_OF)
    conn.close()
    app = create_app(
        db_path=store, token="secret-token", llm_factory=FakeLLM, drain_interval_s=None
    )
    client = TestClient(app)
    cases = client.get("/api/cases").json()
    assert cases and cases[0]["state"] == "action_taken"
    body = client.get(f"/api/cases/{cases[0]['case_id']}").text
    assert "secret-token" not in body


def test_mcp_is_served_over_http_at_slash_mcp(store: Path) -> None:
    """The mount path is the endpoint: POST /mcp (not /mcp/mcp) lists the tools."""
    app = create_app(db_path=store, token="t", llm_factory=FakeLLM, drain_interval_s=None)
    with TestClient(app) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"accept": "application/json, text/event-stream"},
        )
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"investigate", "get_task", "get_case", "list_open_cases", "bench_summary"} <= names


def test_replay_stream_reports_a_failed_investigation_as_an_error_event(store: Path) -> None:
    """A broken LLM (e.g. no API key) must not kill the SSE stream mid-way: the client gets
    `error` then `done` on a 200 response."""

    def broken() -> FakeLLM:
        raise RuntimeError("no credentials")

    app = create_app(db_path=store, token="t", llm_factory=broken, drain_interval_s=None)
    with TestClient(app) as client:
        r = client.get("/api/replay/uri_2021/stream")
    assert r.status_code == 200
    kinds = [ln.split(": ", 1)[1] for ln in r.text.splitlines() if ln.startswith("event: ")]
    assert kinds == ["started", "error", "done"]
    assert "no credentials" in r.text
