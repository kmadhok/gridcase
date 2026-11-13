"""The markets export for the dbt warehouse is written only while something asks for it.

The server is the only process that can read the store, so it writes market metadata out for
dbt. Rewriting the whole file after every 5-minute refresh is wasted when no warehouse build is
running, so the build's freshness gate leaves an ask beside the file, and the server exports
while the newest ask is under two hours old (the build is hourly)."""

from __future__ import annotations

import gzip
import logging
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from gridcase import cli, config
from gridcase.ingest.export import ask_for_markets, export_markets_if_asked
from gridcase.planner.llm import FakeLLM
from gridcase.server.app import create_app
from gridcase.store.db import connect
from tests.polymarket_synth import add_market


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "store.duckdb")
    add_market(conn, "m1", "Sports")
    yield conn
    conn.close()


@pytest.fixture()
def export(tmp_path: Path) -> Path:
    return tmp_path / "exports" / "markets.parquet"


def _ask_was(export: Path, minutes_ago: float) -> None:
    ask_for_markets(export)
    ask = next(p for p in export.parent.iterdir() if p != export)
    at = time.time() - minutes_ago * 60
    os.utime(ask, (at, at))


def test_nothing_is_written_when_nobody_asked(db: duckdb.DuckDBPyConnection, export: Path) -> None:
    assert export_markets_if_asked(db, export) is False
    assert not export.exists()


def test_an_ask_is_answered_by_the_next_refresh(
    db: duckdb.DuckDBPyConnection, export: Path
) -> None:
    ask_for_markets(export)
    assert export_markets_if_asked(db, export) is True
    rows = duckdb.connect().execute("SELECT condition_id FROM read_parquet(?)", [str(export)])
    assert rows.fetchall() == [("m1",)]


def test_an_ask_keeps_the_export_coming_for_two_hours(
    db: duckdb.DuckDBPyConnection, export: Path
) -> None:
    _ask_was(export, minutes_ago=119)
    assert export_markets_if_asked(db, export) is True


def test_an_ask_older_than_two_hours_has_lapsed(
    db: duckdb.DuckDBPyConnection, export: Path
) -> None:
    _ask_was(export, minutes_ago=121)
    assert export_markets_if_asked(db, export) is False
    assert not export.exists()


def test_asking_again_renews_a_lapsed_ask(db: duckdb.DuckDBPyConnection, export: Path) -> None:
    _ask_was(export, minutes_ago=600)
    ask_for_markets(export)
    assert export_markets_if_asked(db, export) is True


def test_the_freshness_gate_asks_so_a_stale_build_passes_on_its_retry(
    db: duckdb.DuckDBPyConnection,
    export: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = tmp_path / "raw" / "polymarket" / "trades" / "2026" / "10" / "02" / "12.jsonl.gz"
    raw.parent.mkdir(parents=True)
    with gzip.open(raw, "wt") as f:
        f.write('{"timestamp": 1790942400}\n')
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "EXPORT_DIR", export.parent)

    assert cli.main(["warehouse", "check-sources"]) == 1  # no export yet: fails, and asks
    assert "markets.parquet" in capsys.readouterr().out
    assert export_markets_if_asked(db, export) is True  # the server's next refresh
    assert cli.main(["warehouse", "check-sources"]) == 0


def test_the_gate_asks_even_when_the_raw_files_are_what_is_stale(
    db: duckdb.DuckDBPyConnection,
    export: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")  # no raw files at all
    monkeypatch.setattr(config, "EXPORT_DIR", export.parent)
    assert cli.main(["warehouse", "check-sources"]) == 1
    assert export_markets_if_asked(db, export) is True


# ------------------------------------------------------------------ the server's own refresh


def _server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A server on an empty store whose background refresh runs once, at start."""
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    return create_app(
        db_path=tmp_path / "server.duckdb",
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        polymarket_interval_s=3600.0,
    )


def _wait_for(done: Callable[[], bool], seconds: float = 10.0) -> bool:
    until = time.monotonic() + seconds
    while time.monotonic() < until and not done():
        time.sleep(0.02)
    return done()


def test_the_server_writes_the_export_when_asked(
    export: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _server(tmp_path, monkeypatch)
    ask_for_markets(export)
    with TestClient(app):
        assert _wait_for(export.exists)


def test_the_server_writes_no_export_when_nobody_asked(
    export: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _server(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger="gridcase")
    with TestClient(app):
        assert _wait_for(lambda: any("polymarket refresh" in r.message for r in caplog.records))
        time.sleep(0.3)  # the export step comes right after that log line
        assert not export.exists()
