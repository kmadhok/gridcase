"""The Polymarket collector reads the v2 trades feed: newest first, 1,000 trades a page, with a
cursor that walks back about a month. Each poll pages back to the newest trade already saved
and appends what it finds to hourly raw files in the v1 field names the loader reads. How far
the record is complete is kept on disk, so a collector that was stopped fills the time it missed
when it starts again; a gap is written down only when the feed cannot reach back that far."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx

from gridcase.ingest.polymarket import (
    FILLED_FILE,
    STATE_FILE,
    CollectorState,
    fill_gaps,
    load_state,
    poll_once,
)
from gridcase.ingest.polymarket_load import load_raw
from gridcase.store.db import connect

T0 = 1790711000  # 2026-09-29T19:43:20Z


def trade(
    ts: int, tx: str, asset: str = "a1", size: float = 10.0, price: float = 0.5
) -> dict[str, Any]:
    """One trade as the v2 feed returns it (snake_case)."""
    return {
        "proxy_wallet": "0xw",
        "side": "BUY",
        "token_id": asset,
        "condition_id": "0xc",
        "size": size,
        "price": price,
        "timestamp": ts,
        "title": "Will X?",
        "slug": "will-x",
        "event_slug": "x",
        "outcome": "Yes",
        "outcome_index": 0,
        "transaction_hash": tx,
    }


class Feed:
    """A fake /v2/trades: serves `pages` in order per walk, chained by cursors. Each call to
    `walk()` queues another set of pages (a later poll). `fail` maps a request number to a status
    code to answer instead."""

    def __init__(self, *walks: list[list[dict[str, Any]]], fail: dict[int, int] | None = None):
        self.walks = list(walks)
        self.fail = fail or {}
        self.requests: list[httpx.Request] = []
        self.current: list[list[dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        n = len(self.requests)
        if n in self.fail:
            return httpx.Response(self.fail[n], headers={"retry-after": "0"})
        cursor = request.url.params.get("cursor")
        if cursor is None:
            self.current = self.walks.pop(0)
            i = 0
        else:
            i = int(cursor)
        more = i + 1 < len(self.current)
        return httpx.Response(
            200,
            json={
                "data": self.current[i],
                "pagination": {"has_more": more, "next_cursor": str(i + 1) if more else None},
            },
        )

    def client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(self.handler),
            base_url="https://data-api.polymarket.com",
        )


def read_raw(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((root / "trades").rglob("*.jsonl.gz")):
        with gzip.open(p, "rt") as f:
            out += [json.loads(line) for line in f]
    return out


def gaps(root: Path) -> list[tuple[int, int]]:
    path = root / "gaps.jsonl"
    if not path.exists():
        return []
    return [(g["from_ts"], g["to_ts"]) for g in map(json.loads, path.read_text().splitlines())]


def saved_state(root: Path) -> int | None:
    path = root / STATE_FILE
    return json.loads(path.read_text())["complete_to"] if path.exists() else None


# --- saving trades -----------------------------------------------------------------------------


def test_new_trades_are_saved_once_to_their_hour_file_in_v1_field_names(tmp_path: Path) -> None:
    first = [[trade(T0 + 10, "t3"), trade(T0 + 5, "t2"), trade(T0, "t1")]]
    second = [[trade(T0 + 20, "t4"), *first[0]]]
    feed = Feed(first, second)
    c, state = feed.client(), CollectorState()
    assert poll_once(c, tmp_path, state, retry_wait=0).new == 3
    r = poll_once(c, tmp_path, state, retry_wait=0)
    assert r.new == 1 and r.gap is False
    rows = read_raw(tmp_path)
    assert [x["transactionHash"] for x in rows] == ["t1", "t2", "t3", "t4"]
    assert rows[0]["proxyWallet"] == "0xw" and rows[0]["asset"] == "a1"
    assert rows[0]["conditionId"] == "0xc" and rows[0]["eventSlug"] == "x"
    assert rows[0]["outcomeIndex"] == 0
    assert not any(k in rows[0] for k in ("proxy_wallet", "token_id", "transaction_hash"))
    assert all("collected_at" in x for x in rows)
    assert (tmp_path / "trades" / "2026" / "09" / "29" / "19.jsonl.gz").exists()


def test_requests_ask_v2_for_full_taker_pages_and_follow_the_cursor(tmp_path: Path) -> None:
    feed = Feed([[trade(T0 + 9, "t9")], [trade(T0 + 5, "t5")], [trade(T0, "t0")]])
    state = CollectorState(last_ts=T0)
    poll_once(feed.client(), tmp_path, state, retry_wait=0)
    first, *rest = feed.requests
    assert first.url.path == "/v2/trades"
    assert first.url.params["limit"] == "1000" and first.url.params["taker_only"] == "true"
    assert "cursor" not in first.url.params
    assert [r.url.params["cursor"] for r in rest] == ["1", "2"]


def test_trades_sharing_a_transaction_are_kept_when_they_differ(tmp_path: Path) -> None:
    """One transaction can fill several outcomes; identity is (tx, asset, side, size, price)."""
    page = [trade(T0, "t1", asset="a1"), trade(T0, "t1", asset="a2")]
    feed = Feed([page], [page])
    c, state = feed.client(), CollectorState()
    assert poll_once(c, tmp_path, state, retry_wait=0).new == 2
    assert poll_once(c, tmp_path, state, retry_wait=0).new == 0


# --- paging back to the last saved trade -------------------------------------------------------


def test_a_poll_pages_back_until_it_reaches_the_last_saved_trade(tmp_path: Path) -> None:
    feed = Feed(
        [
            [trade(T0 + 300, "t6"), trade(T0 + 250, "t5")],
            [trade(T0 + 200, "t4"), trade(T0 + 150, "t3")],
            [trade(T0 + 100, "t2"), trade(T0, "t1")],  # reaches last_ts
            [trade(T0 - 100, "never asked for")],
        ]
    )
    state = CollectorState(last_ts=T0)
    r = poll_once(feed.client(), tmp_path, state, retry_wait=0)
    assert r.gap is False and r.pages == 3
    assert {x["transactionHash"] for x in read_raw(tmp_path)} == {
        "t1",
        "t2",
        "t3",
        "t4",
        "t5",
        "t6",
    }
    assert state.last_ts == T0 + 300 and saved_state(tmp_path) == T0 + 300
    assert gaps(tmp_path) == []


def test_trades_older_than_the_last_saved_one_are_not_saved_again(tmp_path: Path) -> None:
    feed = Feed([[trade(T0 + 60, "new"), trade(T0, "at the mark"), trade(T0 - 60, "old")]])
    state = CollectorState(last_ts=T0)
    assert poll_once(feed.client(), tmp_path, state, retry_wait=0).new == 2
    assert {x["transactionHash"] for x in read_raw(tmp_path)} == {"new", "at the mark"}


def test_the_very_first_poll_takes_one_page_and_does_not_walk_back(tmp_path: Path) -> None:
    feed = Feed([[trade(T0 + 9, "t9")], [trade(T0, "t0")]])
    r = poll_once(feed.client(), tmp_path, CollectorState(), retry_wait=0)
    assert r.pages == 1 and r.new == 1 and gaps(tmp_path) == []


# --- restarts and outages ----------------------------------------------------------------------


def test_a_restarted_collector_fills_the_time_it_missed(tmp_path: Path) -> None:
    before = Feed([[trade(T0, "t1")]])
    poll_once(before.client(), tmp_path, CollectorState(), retry_wait=0)
    # the collector stops; trading goes on for hours; a new process starts from disk
    state = load_state(tmp_path)
    assert state.last_ts == T0
    after = Feed(
        [
            [trade(T0 + 7200, "t4"), trade(T0 + 3600, "t3")],
            [trade(T0 + 1800, "t2"), trade(T0, "t1")],
        ]
    )
    r = poll_once(after.client(), tmp_path, state, retry_wait=0)
    assert r.gap is False and r.new == 3
    # t1 sits exactly at the mark: the walk sees it again but does not save it twice
    assert sorted(x["transactionHash"] for x in read_raw(tmp_path)) == ["t1", "t2", "t3", "t4"]
    hours = sorted(p.name for p in (tmp_path / "trades").rglob("*.jsonl.gz"))
    assert hours == ["19.jsonl.gz", "20.jsonl.gz", "21.jsonl.gz"]
    assert gaps(tmp_path) == []


def test_without_a_state_file_the_collector_resumes_from_the_newest_saved_trade(
    tmp_path: Path,
) -> None:
    hour = tmp_path / "trades" / "2026" / "09" / "29" / "19.jsonl.gz"
    hour.parent.mkdir(parents=True)
    with gzip.open(hour, "wt") as f:
        f.write(json.dumps({"timestamp": T0 + 5, "transactionHash": "a"}) + "\n")
        f.write(json.dumps({"timestamp": T0 + 9, "transactionHash": "b"}) + "\n")
    older = tmp_path / "trades" / "2026" / "09" / "28" / "23.jsonl.gz"
    older.parent.mkdir(parents=True)
    with gzip.open(older, "wt") as f:
        f.write(json.dumps({"timestamp": T0 - 80000, "transactionHash": "z"}) + "\n")
    assert load_state(tmp_path).last_ts == T0 + 9


def test_a_fresh_install_starts_with_no_mark(tmp_path: Path) -> None:
    assert load_state(tmp_path).last_ts is None


def test_the_state_file_wins_over_the_raw_files(tmp_path: Path) -> None:
    """A walk writes newer trades before older ones, so the raw files can run ahead of the mark."""
    (tmp_path / STATE_FILE).write_text(json.dumps({"complete_to": T0}))
    hour = tmp_path / "trades" / "2026" / "09" / "29" / "20.jsonl.gz"
    hour.parent.mkdir(parents=True)
    with gzip.open(hour, "wt") as f:
        f.write(json.dumps({"timestamp": T0 + 3600, "transactionHash": "a"}) + "\n")
    assert load_state(tmp_path).last_ts == T0


# --- gaps --------------------------------------------------------------------------------------


def test_a_feed_that_ends_before_the_last_saved_trade_leaves_a_gap(tmp_path: Path) -> None:
    feed = Feed([[trade(T0 + 900, "t9")], [trade(T0 + 800, "t8")]])
    state = CollectorState(last_ts=T0)
    r = poll_once(feed.client(), tmp_path, state, retry_wait=0)
    assert r.gap is True and r.new == 2
    assert gaps(tmp_path) == [(T0, T0 + 800)]
    assert state.last_ts == T0 + 900 and saved_state(tmp_path) == T0 + 900


def test_a_walk_that_hits_its_page_limit_leaves_a_gap_for_the_rest(tmp_path: Path) -> None:
    feed = Feed([[trade(T0 + 900, "t9")], [trade(T0 + 800, "t8")], [trade(T0, "t0")]])
    state = CollectorState(last_ts=T0)
    r = poll_once(feed.client(), tmp_path, state, max_pages=2, retry_wait=0)
    assert r.gap is True and r.pages == 2
    assert gaps(tmp_path) == [(T0, T0 + 800)]


def test_an_empty_feed_is_not_a_gap(tmp_path: Path) -> None:
    feed = Feed([[]])
    state = CollectorState(last_ts=T0)
    r = poll_once(feed.client(), tmp_path, state, retry_wait=0)
    assert r.new == 0 and r.gap is False and state.last_ts == T0 and gaps(tmp_path) == []


# --- failures ----------------------------------------------------------------------------------


def test_a_failed_request_is_reported_not_raised_and_the_mark_stays(tmp_path: Path) -> None:
    c = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
        base_url="https://data-api.polymarket.com",
    )
    state = CollectorState(last_ts=T0)
    r = poll_once(c, tmp_path, state, retry_wait=0)
    assert r.new == 0 and r.error is not None
    assert state.last_ts == T0 and saved_state(tmp_path) is None and gaps(tmp_path) == []


def test_a_walk_that_fails_midway_is_redone_by_the_next_poll(tmp_path: Path) -> None:
    pages = [[trade(T0 + 900, "t9")], [trade(T0 + 500, "t5")], [trade(T0, "t0")]]
    # requests 1 and 2 succeed, then every retry of page 3 fails (1 try + 3 retries)
    feed = Feed(pages, pages, fail={3: 503, 4: 503, 5: 503, 6: 503})
    c, state = feed.client(), CollectorState(last_ts=T0)
    r = poll_once(c, tmp_path, state, retry_wait=0)
    assert r.error is not None and state.last_ts == T0 and gaps(tmp_path) == []
    r = poll_once(c, tmp_path, state, retry_wait=0)
    assert r.error is None and r.gap is False and state.last_ts == T0 + 900
    assert {x["transactionHash"] for x in read_raw(tmp_path)} == {"t9", "t5", "t0"}


def test_a_brief_failure_is_retried_on_the_same_page(tmp_path: Path) -> None:
    feed = Feed([[trade(T0 + 900, "t9")], [trade(T0, "t0")]], fail={2: 429})
    state = CollectorState(last_ts=T0)
    r = poll_once(feed.client(), tmp_path, state, retry_wait=0)
    assert r.error is None and r.gap is False and r.new == 2
    assert feed.requests[2].url.params["cursor"] == "1"


def test_a_refused_request_is_not_retried(tmp_path: Path) -> None:
    feed = Feed([[trade(T0, "t0")]], fail={1: 400})
    r = poll_once(feed.client(), tmp_path, CollectorState(last_ts=T0), retry_wait=0)
    assert r.error is not None and len(feed.requests) == 1


# --- memory ------------------------------------------------------------------------------------


def test_a_long_catch_up_remembers_only_recent_trades(tmp_path: Path) -> None:
    pages = [[trade(T0 + 86400 - i * 600, f"t{i}")] for i in range(145)]  # a day, newest first
    state = CollectorState(last_ts=T0)
    poll_once(Feed(pages).client(), tmp_path, state, retry_wait=0)
    assert len(read_raw(tmp_path)) == 145
    assert 0 < len(state.seen) <= 4  # only the last 30 minutes' trade identities


# --- the loader reads what the collector writes ------------------------------------------------


def test_v2_trades_load_with_the_same_identity_as_their_v1_copies(tmp_path: Path) -> None:
    """A trade saved by the old v1 collector (size 20) and again from v2 (size 20.0) is one row."""
    hour = tmp_path / "trades" / "2026" / "09" / "29" / "19.jsonl.gz"
    hour.parent.mkdir(parents=True)
    v1 = {
        "proxyWallet": "0xw", "side": "BUY", "asset": "a1", "conditionId": "0xc", "size": 20,
        "price": 0.5, "timestamp": T0, "title": "Will X?", "slug": "will-x", "eventSlug": "x",
        "outcome": "Yes", "transactionHash": "t1", "collected_at": "2026-09-29T19:44:00+00:00",
    }  # fmt: skip
    with gzip.open(hour, "wt") as f:
        f.write(json.dumps(v1) + "\n")
    feed = Feed([[trade(T0 + 60, "t2", size=20.0), trade(T0, "t1", size=20.0)]])
    poll_once(feed.client(), tmp_path, CollectorState(last_ts=T0), retry_wait=0)
    conn = connect(tmp_path / "store.duckdb")
    load_raw(conn, tmp_path)
    rows = conn.execute("SELECT tx_hash, wallet, condition_id FROM trades ORDER BY ts").fetchall()
    assert rows == [("t1", "0xw", "0xc"), ("t2", "0xw", "0xc")]


# --- filling recorded gaps from the feed's history ---------------------------------------------


def write_gaps(root: Path, *spans: tuple[int, int]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "gaps.jsonl").open("a") as f:
        for lo, hi in spans:
            f.write(
                json.dumps({"from_ts": lo, "to_ts": hi, "detected_at": "2026-09-30T00:00:00+00:00"})
                + "\n"
            )


def filled(root: Path) -> list[tuple[int, int]]:
    path = root / FILLED_FILE
    if not path.exists():
        return []
    return [(g["from_ts"], g["to_ts"]) for g in map(json.loads, path.read_text().splitlines())]


def test_filling_saves_only_the_trades_inside_recorded_gaps(tmp_path: Path) -> None:
    write_gaps(tmp_path, (T0 + 100, T0 + 200), (T0 + 400, T0 + 500))
    feed = Feed(
        [
            [trade(T0 + 600, "after"), trade(T0 + 450, "in second")],
            [trade(T0 + 300, "between"), trade(T0 + 200, "at an edge")],
            [trade(T0 + 150, "in first"), trade(T0 + 50, "before")],
        ]
    )
    r = fill_gaps(feed.client(), tmp_path, retry_wait=0)
    assert r.saved == 3 and r.error is None
    assert {x["transactionHash"] for x in read_raw(tmp_path)} == {
        "in second", "at an edge", "in first",
    }  # fmt: skip
    assert all("collected_at" in x and "asset" in x for x in read_raw(tmp_path))
    assert sorted(filled(tmp_path)) == [(T0 + 100, T0 + 200), (T0 + 400, T0 + 500)]
    assert gaps(tmp_path) == [(T0 + 100, T0 + 200), (T0 + 400, T0 + 500)]  # append-only


def test_filling_stops_once_it_is_past_the_oldest_gap(tmp_path: Path) -> None:
    write_gaps(tmp_path, (T0 + 100, T0 + 200))
    feed = Feed([[trade(T0 + 300, "a")], [trade(T0 + 90, "b")], [trade(T0, "never asked for")]])
    r = fill_gaps(feed.client(), tmp_path, retry_wait=0)
    assert len(feed.requests) == 2 and r.pages == 2


def test_a_gap_the_walk_did_not_get_past_is_not_marked_filled(tmp_path: Path) -> None:
    write_gaps(tmp_path, (T0 + 100, T0 + 200), (T0 + 400, T0 + 500))
    feed = Feed([[trade(T0 + 450, "x")], [trade(T0 + 150, "y")]])  # the feed ends at T0 + 150
    r = fill_gaps(feed.client(), tmp_path, retry_wait=0)
    assert filled(tmp_path) == [(T0 + 400, T0 + 500)] and r.unfilled == 1


def test_a_page_limit_leaves_older_gaps_unfilled(tmp_path: Path) -> None:
    write_gaps(tmp_path, (T0 + 100, T0 + 200), (T0 + 400, T0 + 500))
    feed = Feed([[trade(T0 + 450, "x")], [trade(T0 + 300, "y")], [trade(T0 + 150, "z")]])
    fill_gaps(feed.client(), tmp_path, max_pages=2, retry_wait=0)
    assert filled(tmp_path) == [(T0 + 400, T0 + 500)]


def test_a_failure_keeps_the_gaps_walked_past_and_leaves_the_rest(tmp_path: Path) -> None:
    write_gaps(tmp_path, (T0 + 100, T0 + 200), (T0 + 400, T0 + 500))
    pages = [[trade(T0 + 450, "x")], [trade(T0 + 350, "y")], [trade(T0 + 150, "z")]]
    feed = Feed(pages, fail={3: 503, 4: 503, 5: 503, 6: 503})
    r = fill_gaps(feed.client(), tmp_path, retry_wait=0)
    assert r.error is not None and filled(tmp_path) == [(T0 + 400, T0 + 500)]


def test_a_gap_already_filled_is_not_walked_for_again(tmp_path: Path) -> None:
    write_gaps(tmp_path, (T0 + 100, T0 + 200))
    fill_gaps(Feed([[trade(T0 + 150, "y"), trade(T0 + 50, "z")]]).client(), tmp_path, retry_wait=0)
    feed = Feed()
    r = fill_gaps(feed.client(), tmp_path, retry_wait=0)
    assert feed.requests == [] and r.pages == 0 and filled(tmp_path) == [(T0 + 100, T0 + 200)]


def test_with_no_gaps_nothing_is_fetched(tmp_path: Path) -> None:
    feed = Feed()
    assert fill_gaps(feed.client(), tmp_path, retry_wait=0).pages == 0 and feed.requests == []


def test_the_store_drops_a_gap_once_it_is_filled(tmp_path: Path) -> None:
    write_gaps(tmp_path, (T0 + 100, T0 + 200), (T0 + 400, T0 + 500))
    conn = connect(tmp_path / "store.duckdb")
    load_raw(conn, tmp_path)
    assert conn.execute("SELECT count(*) FROM collector_gaps").fetchone() == (2,)
    feed = Feed([[trade(T0 + 450, "x")], [trade(T0 + 300, "y")]])
    fill_gaps(feed.client(), tmp_path, retry_wait=0)  # walks past the newer gap only
    load_raw(conn, tmp_path)
    left = conn.execute("SELECT epoch(gap_from)::BIGINT FROM collector_gaps").fetchall()
    assert left == [(T0 + 100,)]
    assert conn.execute("SELECT count(*) FROM trades").fetchone() == (1,)
    load_raw(conn, tmp_path)  # loading again changes nothing
    assert conn.execute("SELECT count(*) FROM collector_gaps").fetchone() == (1,)
