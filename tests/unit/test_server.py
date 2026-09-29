"""Server internals: task registry, read-only tool exposure, scheduled re-check drain."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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


def test_map_skips_hours_whose_forecast_is_still_being_revised(store: Path) -> None:
    """EIA publishes aggregate forecasts incrementally (US48 at 10:00Z on 2026-09-29 read
    5,104 → 95,514 → 156,731 → 406,123 MW across hourly pulls). The map must show the latest
    hour whose forecast has settled, not a partial sum that makes a 157% miss."""
    from gridcase.store.db import append_observations
    from gridcase.store.models import Observation

    now = datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    hot, old = now - timedelta(hours=1), now - timedelta(hours=2)

    def obs(series: str, t: datetime, value: float, seen: datetime) -> Observation:
        return Observation(
            series_id=series,
            entity_id="PJM",
            valid_time=t,
            value=value,
            observed_at=seen,
            available_at=seen,
            source="eia",
            vintage="observed",
            raw_hash="x",
        )

    conn = connect(store)
    append_observations(
        conn,
        [
            # the settled hour: one forecast vintage, seen long ago
            obs("eia.D", old, 400_000, old + timedelta(minutes=17)),
            obs("eia.DF", old, 398_000, old - timedelta(hours=20)),
            # the latest hour: forecast revised from a partial sum within the last 3 h
            obs("eia.D", hot, 404_109, hot + timedelta(minutes=17)),
            obs("eia.DF", hot, 95_514, hot - timedelta(minutes=50)),
            obs("eia.DF", hot, 156_731, hot + timedelta(minutes=17)),
        ],
    )
    conn.close()
    app = create_app(db_path=store, token="t", llm_factory=FakeLLM, drain_interval_s=None)
    with TestClient(app) as client:
        pjm = next(r for r in client.get("/api/map").json()["regions"] if r["entity"] == "PJM")
    assert pjm["latest_hour"] == old.isoformat()
    assert pjm["forecast_mw"] == 398_000
    assert abs(pjm["forecast_error_pct"] - 0.5) < 0.01


def test_map_treats_the_us48_aggregate_as_settled_only_after_a_day(store: Path) -> None:
    """EIA's US48 day-ahead forecast is a computed sum that keeps filling in for ~a day
    (02-05Z on 2026-09-29 sat 10% low for 10+ hours with no further revision). For the
    aggregate the tile shows the latest hour at least 24 h old."""
    from gridcase.store.db import append_observations
    from gridcase.store.models import Observation

    now = datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    fresh, day_old = now - timedelta(hours=4), now - timedelta(hours=25)

    def obs(series: str, t: datetime, value: float) -> Observation:
        seen = t - timedelta(hours=20) if series == "eia.DF" else t + timedelta(minutes=17)
        return Observation(
            series_id=series,
            entity_id="US48",
            valid_time=t,
            value=value,
            observed_at=seen,
            available_at=seen,
            source="eia",
            vintage="observed",
            raw_hash="x",
        )

    conn = connect(store)
    append_observations(
        conn,
        [
            obs("eia.D", day_old, 450_000),
            obs("eia.DF", day_old, 445_500),
            obs("eia.D", fresh, 480_040),
            obs("eia.DF", fresh, 431_921),  # partial sum, unrevised
        ],
    )
    conn.close()
    app = create_app(db_path=store, token="t", llm_factory=FakeLLM, drain_interval_s=None)
    with TestClient(app) as client:
        us48 = next(r for r in client.get("/api/map").json()["regions"] if r["entity"] == "US48")
    assert us48["latest_hour"] == day_old.isoformat()
    assert abs(us48["forecast_error_pct"] - 1.01) < 0.01


def test_map_skips_placeholder_hours_where_demand_equals_the_forecast(store: Path) -> None:
    """EIA's first CAISO demand value for an hour is the forecast itself (29,952 → 29,842
    on revision); an hour whose demand equals its forecast to the MW is not real yet."""
    from gridcase.store.db import append_observations
    from gridcase.store.models import Observation

    now = datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    hot, old = now - timedelta(hours=1), now - timedelta(hours=2)

    def obs(series: str, t: datetime, value: float) -> Observation:
        seen = t - timedelta(hours=20) if series == "eia.DF" else t + timedelta(minutes=17)
        return Observation(
            series_id=series,
            entity_id="CISO",
            valid_time=t,
            value=value,
            observed_at=seen,
            available_at=seen,
            source="eia",
            vintage="observed",
            raw_hash="x",
        )

    conn = connect(store)
    append_observations(
        conn,
        [
            obs("eia.D", old, 27_959),
            obs("eia.DF", old, 28_172),
            obs("eia.D", hot, 26_097),
            obs("eia.DF", hot, 26_097),  # placeholder
        ],
    )
    conn.close()
    app = create_app(db_path=store, token="t", llm_factory=FakeLLM, drain_interval_s=None)
    with TestClient(app) as client:
        ciso = next(r for r in client.get("/api/map").json()["regions"] if r["entity"] == "CISO")
    assert ciso["latest_hour"] == old.isoformat()


def test_map_reports_each_regions_usual_gap_for_context(store: Path) -> None:
    """A 9% gap means little without knowing the region usually runs +1.5%: the API returns
    the trailing-28-day median error so the tile can say 'usually +1.5%'."""
    from gridcase.store.db import append_observations
    from gridcase.store.models import Observation

    now = datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    rows = []
    for k in range(1, 30 * 24):  # a month of hours running exactly +2% (most recent at +9%)
        t = now - timedelta(hours=k + 25)
        pct = 0.09 if k == 1 else 0.02
        rows.append(
            Observation(
                series_id="eia.DF",
                entity_id="US48",
                valid_time=t,
                value=400_000,
                observed_at=t - timedelta(hours=20),
                available_at=t - timedelta(hours=20),
                source="eia",
                vintage="observed",
                raw_hash="x",
            )
        )
        rows.append(
            Observation(
                series_id="eia.D",
                entity_id="US48",
                valid_time=t,
                value=400_000 * (1 + pct),
                observed_at=t + timedelta(minutes=17),
                available_at=t + timedelta(minutes=17),
                source="eia",
                vintage="observed",
                raw_hash="x",
            )
        )
    conn = connect(store)
    append_observations(conn, rows)
    conn.close()
    app = create_app(db_path=store, token="t", llm_factory=FakeLLM, drain_interval_s=None)
    with TestClient(app) as client:
        us48 = next(r for r in client.get("/api/map").json()["regions"] if r["entity"] == "US48")
    assert abs(us48["forecast_error_pct"] - 9.0) < 0.01
    assert abs(us48["typical_error_pct"] - 2.0) < 0.01
