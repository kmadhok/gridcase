"""Looking for markets that still need a category does not re-read the whole record each time.

Every market in the store must end up with a category. A full pass looks through every trade for
markets without one. Once a full pass has found none left, a market can only be missing if it
traded in the hours just loaded, so a scoped pass looks there only. A lookup that failed, or a
pass that broke, leaves markets behind anywhere, so the next pass is full again."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from gridcase.ingest.polymarket_load import refresh, refresher
from gridcase.store.db import connect
from tests.polymarket_synth import add_trades

EARLY = datetime(2026, 10, 1, 3)
LATER = datetime(2026, 10, 9, 12)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "store.duckdb")
    yield conn
    conn.close()


def hour_file(root: Path, hour: datetime, markets: list[str]) -> None:
    """The collector's file for `hour`, holding one trade in each market."""
    path = root / "trades" / f"{hour:%Y/%m/%d/%H}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = (hour + timedelta(minutes=5)).replace(tzinfo=UTC).isoformat()
    with gzip.open(path, "wt") as f:
        for i, cid in enumerate(markets):
            t: dict[str, Any] = {
                "proxyWallet": "0xw", "side": "BUY", "asset": f"{cid}-yes", "conditionId": cid,
                "size": 10.0, "price": 0.5, "title": "Will X?", "slug": "will-x",
                "timestamp": int(hour.replace(tzinfo=UTC).timestamp()) + i,
                "eventSlug": "x", "outcome": "Yes", "transactionHash": f"tx-{cid}",
                "collected_at": seen,
            }  # fmt: skip
            f.write(json.dumps(t) + "\n")


class Gamma:
    """Polymarket's metadata API: every market is Politics. `down` answers 503; `broken`
    raises something the lookup does not expect."""

    def __init__(self) -> None:
        self.down = self.broken = False
        self.calls = 0
        self.client = httpx.Client(
            transport=httpx.MockTransport(self._handle), base_url="https://gamma.test"
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.broken:
            raise RuntimeError("boom")
        if self.down:
            return httpx.Response(503)
        params = request.url.params
        if request.url.path == "/markets":
            cids = params.get_list("condition_ids")
            return httpx.Response(
                200, json=[{"conditionId": c, "question": "q", "events": []} for c in cids]
            )
        return httpx.Response(
            200,
            json=[
                {"slug": s, "title": s, "tags": [{"label": "Politics"}]}
                for s in params.get_list("slug")
            ],
        )


def categorised(conn: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT condition_id FROM markets").fetchall()}


def an_uncategorised_market_traded_early(conn: duckdb.DuckDBPyConnection) -> None:
    add_trades(conn, [{"ts": EARLY, "wallet": "w", "usd": 5.0, "condition_id": "early"}])


# ------------------------------------------------------------------ one pass


def test_a_scoped_pass_categorises_the_markets_just_loaded(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    hour_file(tmp_path, LATER, ["a", "b"])
    r = refresh(db, tmp_path, Gamma().client, full=False)
    assert categorised(db) == {"a", "b"} and r.markets_added == 2 and not r.lookups_failed


def test_a_scoped_pass_looks_only_at_the_hours_just_loaded(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    an_uncategorised_market_traded_early(db)
    hour_file(tmp_path, LATER, ["a"])
    refresh(db, tmp_path, Gamma().client, full=False)
    assert categorised(db) == {"a"}


def test_a_pass_is_full_unless_asked_otherwise(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    an_uncategorised_market_traded_early(db)
    hour_file(tmp_path, LATER, ["a"])
    refresh(db, tmp_path, Gamma().client)
    assert categorised(db) == {"a", "early"}


def test_a_scoped_pass_with_nothing_new_asks_nothing(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    hour_file(tmp_path, LATER, ["a"])
    gamma = Gamma()
    refresh(db, tmp_path, gamma.client, full=False)
    asked = gamma.calls
    r = refresh(db, tmp_path, gamma.client, full=False)
    assert gamma.calls == asked and r.markets_added == 0 and not r.lookups_failed


def test_a_file_arriving_late_for_an_old_hour_is_covered(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    gamma = Gamma()
    hour_file(tmp_path, LATER, ["a"])
    refresh(db, tmp_path, gamma.client, full=False)
    hour_file(tmp_path, EARLY, ["late"])  # a repaired file, or a filled gap
    refresh(db, tmp_path, gamma.client, full=False)
    assert categorised(db) == {"a", "late"}


def test_an_hour_file_that_grew_is_covered_again(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    gamma = Gamma()
    hour_file(tmp_path, LATER, ["a"])
    refresh(db, tmp_path, gamma.client, full=False)
    hour_file(tmp_path, LATER, ["a", "b"])  # the collector appends to the current hour
    refresh(db, tmp_path, gamma.client, full=False)
    assert categorised(db) == {"a", "b"}


def test_a_failed_lookup_is_reported_and_the_market_left_for_later(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    gamma = Gamma()
    gamma.down = True
    hour_file(tmp_path, LATER, ["a"])
    r = refresh(db, tmp_path, gamma.client, full=False)
    assert r.lookups_failed and r.markets_added == 0 and categorised(db) == set()


# ------------------------------------------------------------------ a long-running caller


def test_the_first_pass_after_a_start_is_full(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    an_uncategorised_market_traded_early(db)  # left behind by the previous process
    hour_file(tmp_path, LATER, ["a"])
    refresher(tmp_path, Gamma().client)(db)
    assert categorised(db) == {"a", "early"}


def test_passes_after_a_clean_one_are_scoped(db: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    run = refresher(tmp_path, Gamma().client)
    run(db)
    an_uncategorised_market_traded_early(db)  # cannot happen live: shows where a pass looks
    hour_file(tmp_path, LATER, ["a"])
    run(db)
    assert categorised(db) == {"a"}


def test_after_a_failed_lookup_the_next_pass_is_full(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    gamma = Gamma()
    run = refresher(tmp_path, gamma.client)
    run(db)
    gamma.down = True
    hour_file(tmp_path, LATER, ["a"])
    assert run(db).lookups_failed and categorised(db) == set()
    gamma.down = False
    run(db)  # no new file: only a full pass can find the market
    assert categorised(db) == {"a"}


def test_after_a_pass_that_broke_the_next_is_full(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    gamma = Gamma()
    run = refresher(tmp_path, gamma.client)
    run(db)
    gamma.broken = True
    hour_file(tmp_path, LATER, ["a"])
    with pytest.raises(RuntimeError):
        run(db)  # the trades are loaded, the market is not categorised
    gamma.broken = False
    run(db)
    assert categorised(db) == {"a"}


def test_a_full_pass_that_finds_nothing_is_followed_by_scoped_ones(
    db: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    gamma = Gamma()
    run = refresher(tmp_path, gamma.client)
    run(db)
    run(db)
    assert gamma.calls == 0
