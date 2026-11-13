"""One page refresh reads each thing once.

The Now page and the catalog check the same series several times at one moment (the alarm, the
48-hour chart, the catalog's status), and no trades load in between. So within one refresh the
whole-record lookup of where the record starts and ends runs once, and each series' newest
hours are queried once. The pages themselves are unchanged."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from gridcase.market_alarm import clear_cache, load_watch
from gridcase.planner.llm import FakeLLM
from gridcase.server import app as server
from gridcase.server.app import create_app
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF, add_trades, one_market_tripled

SPAN = "SELECT min(ts), max(ts) FROM trades_as_of"
HOURLY = "date_trunc('hour', ts)"


class Noting:
    """A store connection that notes every statement run on it or on its cursors."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, seen: list[str]) -> None:
        self._conn, self._seen = conn, seen

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self._seen.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def cursor(self) -> Noting:
        return Noting(self._conn.cursor(), self._seen)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _app(store: Path, snapshots: bool) -> Any:
    return create_app(
        db_path=store,
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        clock=lambda: AS_OF,
        snapshot_pages=snapshots,
    )


@pytest.fixture()
def seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement the server runs on its store."""
    statements: list[str] = []
    monkeypatch.setattr(server, "connect", lambda path: Noting(connect(path), statements))
    clear_cache()
    return statements


def test_a_refresh_reads_the_record_span_once(market_store: Path, seen: list[str]) -> None:
    app = _app(market_store, snapshots=True)
    with TestClient(app):
        seen.clear()
        app.state.refresh_pages()
    assert sum(SPAN in s for s in seen) == 1


def test_a_refresh_queries_each_series_newest_hours_once(
    market_store: Path, seen: list[str]
) -> None:
    app = _app(market_store, snapshots=True)
    with TestClient(app):
        app.state.refresh_pages()  # settled hours are remembered from here on
        seen.clear()
        app.state.refresh_pages()
    hourly = [s for s in seen if HOURLY in s]
    assert len(hourly) == len(load_watch()[0]) == len(set(hourly))


def test_the_first_refresh_after_a_start_repeats_no_query(
    market_store: Path, seen: list[str]
) -> None:
    app = _app(market_store, snapshots=True)
    with TestClient(app):
        seen.clear()
        app.state.refresh_pages()
    hourly = [s for s in seen if HOURLY in s]
    assert len(hourly) == len(set(hourly))


def test_the_now_snapshot_matches_a_fresh_computation(market_store: Path) -> None:
    fresh = _app(market_store, snapshots=False)
    with TestClient(fresh) as client:
        expected = client.get("/api/now").json()
    fresh.state.conn.close()
    snap = _app(market_store, snapshots=True)
    with TestClient(snap) as client:
        snap.state.refresh_pages()
        assert client.get("/api/now").json() == expected


def test_an_empty_store_refreshes(tmp_path: Path) -> None:
    app = _app(tmp_path / "empty.duckdb", snapshots=True)
    with TestClient(app) as client:
        app.state.refresh_pages()
        now = client.get("/api/now").json()
    assert {s["status"] for s in now["series"]} == {"learning"}


def test_what_one_refresh_read_is_not_reused_by_the_next(market_store: Path) -> None:
    app = _app(market_store, snapshots=True)
    with TestClient(app) as client:
        app.state.refresh_pages()
        before = client.get("/api/now").json()
        c = app.state.conn.cursor()
        add_trades(c, one_market_tripled())
        c.close()
        app.state.refresh_pages()  # same clock, same queries, new trades
        after = client.get("/api/now").json()
    assert after != before


def test_a_request_outside_a_refresh_reads_the_store_each_time(market_store: Path) -> None:
    app = _app(market_store, snapshots=False)
    with TestClient(app) as client:
        client.get("/api/now")
        c = app.state.conn.cursor()
        add_trades(c, one_market_tripled())
        c.close()
        now = client.get("/api/now").json()
    sports = next(s for s in now["series"] if s["segment"] == "category=Sports")
    assert sports["status"] == "alarm"
