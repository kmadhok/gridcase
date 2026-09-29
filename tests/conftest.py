"""Shared fixtures: a synthetic 9-day Polymarket store (see tests/polymarket_synth.py), built
once per session and copied for each test that needs its own."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gridcase.store.db import connect
from tests.polymarket_synth import build_market_store


@pytest.fixture(scope="session")
def market_store_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("market") / "template.duckdb"
    conn = connect(path)
    build_market_store(conn)
    conn.close()
    return path


@pytest.fixture()
def market_store(market_store_template: Path, tmp_path: Path) -> Path:
    """A private copy of the synthetic market store for one test."""
    path = tmp_path / "market.duckdb"
    shutil.copy(market_store_template, path)
    return path
