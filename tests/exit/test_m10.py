"""M10: source freshness and publishing a Hex-ready copy without touching the store."""

from __future__ import annotations

import gzip
import os
import time
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from gridcase import cli, config


@pytest.fixture()
def sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    raw = tmp_path / "raw" / "polymarket" / "trades" / "2026" / "10" / "02" / "12.jsonl.gz"
    raw.parent.mkdir(parents=True)
    with gzip.open(raw, "wt") as f:
        f.write('{"timestamp": 1790942400}\n')
    export = tmp_path / "exports" / "markets.parquet"
    export.parent.mkdir()
    with duckdb.connect() as conn:
        conn.execute("COPY (SELECT 'market' AS condition_id) TO ? (FORMAT PARQUET)", [str(export)])
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "EXPORT_DIR", export.parent)
    return raw, export


def _age(path: Path, minutes: int) -> None:
    mtime = time.time() - minutes * 60
    os.utime(path, (mtime, mtime))


def test_check_sources_fresh_uses_newest_mtime(sources: tuple[Path, Path]) -> None:
    raw, _ = sources
    older = raw.with_name("23.jsonl.gz")
    older.write_bytes(raw.read_bytes())
    _age(older, 60)  # lexically last is not the newest file
    assert cli.main(["warehouse", "check-sources"]) == 0


@pytest.mark.parametrize("source", ["raw", "export"])
def test_check_sources_stale(
    sources: tuple[Path, Path], source: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _age(sources[0 if source == "raw" else 1], 16)
    assert cli.main(["warehouse", "check-sources"]) == 1
    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    assert "stale" in output[0].lower()
    assert ("raw" if source == "raw" else "markets.parquet") in output[0]


@pytest.mark.parametrize("source", ["raw", "export"])
def test_check_sources_missing(
    sources: tuple[Path, Path], source: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sources[0 if source == "raw" else 1].unlink()
    # A fresh non-trade gzip file must not disguise a missing trade feed.
    (config.RAW_DIR / "polymarket" / "heartbeat.jsonl.gz").touch()
    assert cli.main(["warehouse", "check-sources"]) == 1
    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    assert "missing" in output[0].lower()


def test_check_sources_custom_limit(sources: tuple[Path, Path]) -> None:
    for path in sources:
        _age(path, 20)
    assert cli.main(["warehouse", "check-sources", "--max-age-minutes", "30"]) == 0
    assert cli.main(["warehouse", "check-sources", "--max-age-minutes", "10"]) == 1


def test_publish_copies_and_replaces_all_three_marts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    warehouse = tmp_path / "warehouse's.duckdb"
    target = tmp_path / "published.duckdb"
    tables = {
        "metrics_daily": "SELECT DATE '2026-10-01' AS day, 1200.0 AS volume_usd, 2 AS trades",
        "metrics_hourly_by_category": """
            SELECT TIMESTAMP '2026-10-01 12:00:00' AS hour, DATE '2026-10-01' AS day,
                   'Sports' AS category, 1200.0 AS volume_usd, 2 AS trades,
                   2 AS active_traders, 0.9 AS whale_share, 0.5 AS buy_share
        """,
        "dim_markets": """
            SELECT * FROM (VALUES ('m1', 'A market', 'Sports'), ('m2', 'Another', 'Other'))
                t(condition_id, market_name, category)
        """,
    }
    with duckdb.connect(str(warehouse)) as conn:
        conn.execute("CREATE SCHEMA main_marts")
        for name, query in tables.items():
            conn.execute(f"CREATE TABLE main_marts.{name} AS {query}")
    monkeypatch.setattr(config, "WAREHOUSE_PATH", warehouse)
    monkeypatch.setenv("MARKETCASE_PUBLISH_TARGET", str(target))
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    for _ in range(2):  # retries replace stale contents and are safe to repeat
        with duckdb.connect(str(target)) as conn:
            for name in tables:
                conn.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT -1 AS stale")
        assert cli.main(["warehouse", "publish"]) == 0
        output = capsys.readouterr().out
        with (
            duckdb.connect(str(target), read_only=True) as published,
            duckdb.connect(str(warehouse), read_only=True) as source,
        ):
            for name in tables:
                expected = source.execute(f"SELECT * FROM main_marts.{name} ORDER BY 1").fetchall()
                assert published.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() == expected
                assert f"{name}: {len(expected)} rows" in output


@pytest.mark.parametrize("target", [None, "md:another_database"])
def test_publish_without_token_never_connects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str | None
) -> None:
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.delenv("MARKETCASE_PUBLISH_TARGET", raising=False)
    if target is not None:
        monkeypatch.setenv("MARKETCASE_PUBLISH_TARGET", target)
    with patch("duckdb.connect") as connect:
        assert cli.main(["warehouse", "publish"]) == 0
        connect.assert_not_called()
    assert capsys.readouterr().out == "publish skipped: no MOTHERDUCK_TOKEN\n"
