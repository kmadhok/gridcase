"""M1 exit: collected Polymarket trades become a clean, leak-free store.

Fixture: one real hour of raw collector output (2026-09-29 19:xx UTC, 28,989 lines holding
24,989 distinct trades: the collector's first restart wrote some twice)."""

from __future__ import annotations

import gzip
import json
import os
import shutil
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from gridcase.ingest.polymarket_load import enrich_markets, load_raw
from gridcase.store.db import connect

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "polymarket"
HOUR_FILE = Path("trades/2026/09/29/19.jsonl.gz")
UNIQUE = 24_989


@pytest.fixture()
def raw(tmp_path: Path) -> Path:
    root = tmp_path / "raw"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "m1.duckdb")
    yield conn
    conn.close()


def _count(db: duckdb.DuckDBPyConnection, sql: str = "SELECT count(*) FROM trades") -> int:
    row = db.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def test_1_each_trade_is_stored_once_however_often_it_is_loaded(
    db: duckdb.DuckDBPyConnection, raw: Path
) -> None:
    first = load_raw(db, raw)
    assert first.new == UNIQUE and _count(db) == UNIQUE
    assert load_raw(db, raw).new == 0 and _count(db) == UNIQUE


def test_2_a_file_rewritten_after_a_collector_restart_adds_nothing(
    db: duckdb.DuckDBPyConnection, raw: Path
) -> None:
    load_raw(db, raw)
    path = raw / HOUR_FILE
    with gzip.open(path, "rt") as f:
        lines = f.read().splitlines()[:5000]
    with gzip.open(path, "at") as f:  # a restarted collector re-saves trades it already had
        f.write("\n".join(lines) + "\n")
    assert load_raw(db, raw).new == 0 and _count(db) == UNIQUE


def test_3_dollars_are_size_times_price(db: duckdb.DuckDBPyConnection, raw: Path) -> None:
    load_raw(db, raw)
    bad = _count(db, "SELECT count(*) FROM trades WHERE abs(usd - size * price) > 1e-9")
    total = db.execute("SELECT sum(usd) FROM trades").fetchone()
    assert bad == 0 and total is not None and 1_000_000 < total[0] < 1_500_000


def test_4_collector_gaps_are_recorded(db: duckdb.DuckDBPyConnection, raw: Path) -> None:
    (raw / "gaps.jsonl").write_text(
        json.dumps(
            {"from_ts": 1790711000, "to_ts": 1790711800, "detected_at": "2026-09-29T19:53:00+00:00"}
        )
        + "\n"
    )
    load_raw(db, raw)
    load_raw(db, raw)
    rows = db.execute("SELECT gap_from, gap_to FROM collector_gaps").fetchall()
    assert rows == [(datetime(2026, 9, 29, 19, 43, 20), datetime(2026, 9, 29, 19, 56, 40))]


def test_5_nothing_collected_after_as_of_is_visible(
    db: duckdb.DuckDBPyConnection, raw: Path
) -> None:
    load_raw(db, raw)
    first_seen = db.execute("SELECT min(collected_at), max(collected_at) FROM trades").fetchone()
    assert first_seen is not None
    lo, hi = first_seen
    q = "SELECT count(*), max(collected_at) FROM trades_as_of(?)"
    none = db.execute(q, [lo.replace(second=0) if lo.second else lo]).fetchone()
    assert none is not None and (none[1] is None or none[1] <= lo)
    everything = db.execute(q, [hi]).fetchone()
    assert everything is not None and everything[0] == UNIQUE


def _fake_gamma(tags_by_event: dict[str, list[str]]) -> httpx.Client:
    """Condition id -> event slug via /markets; event slug -> tags via /events."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.url.path == "/markets":
            cids = params.get_list("condition_ids")
            return httpx.Response(
                200,
                json=[
                    {"conditionId": c, "question": "q", "events": [{"slug": f"ev-{c[-4:]}"}]}
                    for c in cids
                ],
            )
        if request.url.path == "/events":
            return httpx.Response(
                200,
                json=[
                    {
                        "slug": s,
                        "title": s,
                        "tags": [{"label": t} for t in tags_by_event.get(s, [])],
                    }
                    for s in params.get_list("slug")
                ],
            )
        return httpx.Response(404)

    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://gamma-api.polymarket.com"
    )


def test_6_every_market_gets_a_category(db: duckdb.DuckDBPyConnection, raw: Path) -> None:
    load_raw(db, raw)
    cids = [
        r[0] for r in db.execute("SELECT DISTINCT condition_id FROM trades ORDER BY 1").fetchall()
    ]
    tags = {
        f"ev-{c[-4:]}": ["Politics"] if i % 2 else ["Crypto", "Bitcoin"] for i, c in enumerate(cids)
    }
    enrich_markets(db, _fake_gamma(tags))
    missing = _count(
        db,
        "SELECT count(DISTINCT t.condition_id) FROM trades t "
        "LEFT JOIN markets m USING (condition_id) WHERE m.category IS NULL",
    )
    assert missing == 0
    cats = dict(db.execute("SELECT category, count(*) FROM markets GROUP BY 1").fetchall())
    assert set(cats) <= {"Politics", "Crypto"}
    assert enrich_markets(db, _fake_gamma(tags)) == 0  # nothing new to look up


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GRIDCASE_LIVE"), reason="calls gamma-api.polymarket.com")
def test_7_real_categories_cover_nearly_all_volume(
    db: duckdb.DuckDBPyConnection, raw: Path
) -> None:
    load_raw(db, raw)
    enrich_markets(db, httpx.Client(base_url="https://gamma-api.polymarket.com"))
    row: Any = db.execute(
        "SELECT sum(usd) FILTER (WHERE m.category <> 'Other') / sum(usd) "
        "FROM trades t JOIN markets m USING (condition_id)"
    ).fetchone()
    assert row[0] >= 0.95


def test_8_the_server_refresh_loads_and_categorises_in_one_step(
    db: duckdb.DuckDBPyConnection, raw: Path
) -> None:
    from gridcase.ingest.polymarket_load import refresh

    cids_tags: dict[str, list[str]] = {}
    first = refresh(db, raw, _fake_gamma(cids_tags))
    assert first.new == UNIQUE and first.markets_added > 1000
    again = refresh(db, raw, _fake_gamma(cids_tags))
    assert again.new == 0 and again.markets_added == 0
