"""The Now page and the KPI catalog are computed once per data refresh and served from a snapshot.

Every visitor between two refreshes sees the same numbers, so the server computes them in the
background after each refresh instead of once per request, and keeps the last snapshot in the
store so a restart serves it at once. With snapshots off (the default), every request is
computed fresh, as before."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from gridcase.planner.llm import FakeLLM
from gridcase.server.app import create_app
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF, add_trades, one_market_tripled


def _app(store: Path, snapshots: bool) -> Any:
    return create_app(
        db_path=store,
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        clock=lambda: AS_OF,
        snapshot_pages=snapshots,
    )


def _sports(now: dict[str, Any]) -> str:
    return str(next(s for s in now["series"] if s["segment"] == "category=Sports")["status"])


def test_snapshot_serves_the_last_refresh_until_the_next_one(market_store: Path) -> None:
    app = _app(market_store, snapshots=True)
    with TestClient(app) as client:
        app.state.refresh_pages()
        before = client.get("/api/now").json()
        assert _sports(before) != "alarm"
        c = app.state.conn.cursor()
        add_trades(c, one_market_tripled())
        c.close()
        assert client.get("/api/now").json() == before  # new trades wait for the next refresh
        app.state.refresh_pages()
        after = client.get("/api/now").json()
    assert _sports(after) == "alarm" and after["cases"] == before["cases"]


def test_a_restart_serves_the_stored_snapshot(market_store: Path) -> None:
    first = _app(market_store, snapshots=True)
    with TestClient(first) as client:
        first.state.refresh_pages()
        now, cat = client.get("/api/now").json(), client.get("/api/catalog").json()
    first.state.conn.close()
    conn = connect(market_store)
    add_trades(conn, one_market_tripled())
    conn.close()
    with TestClient(_app(market_store, snapshots=True)) as client:
        assert client.get("/api/now").json() == now
        assert client.get("/api/catalog").json() == cat


def test_the_catalog_snapshot_matches_a_fresh_computation(market_store: Path) -> None:
    fresh = _app(market_store, snapshots=False)
    with TestClient(fresh) as client:
        expected = client.get("/api/catalog").json()
    fresh.state.conn.close()
    snap = _app(market_store, snapshots=True)
    with TestClient(snap) as client:
        snap.state.refresh_pages()
        assert client.get("/api/catalog").json() == expected


def test_without_a_snapshot_yet_a_request_computes_once(market_store: Path) -> None:
    with TestClient(_app(market_store, snapshots=True)) as client:
        r = client.get("/api/now")
        assert r.status_code == 200 and len(r.json()["series"]) == 18
        assert client.get("/api/now").json() == r.json()


def test_without_snapshots_every_request_is_fresh(market_store: Path) -> None:
    app = _app(market_store, snapshots=False)
    with TestClient(app) as client:
        before = client.get("/api/now").json()
        c = app.state.conn.cursor()
        add_trades(c, one_market_tripled())
        c.close()
        after = client.get("/api/now").json()
    assert _sports(before) != "alarm" and _sports(after) == "alarm"
