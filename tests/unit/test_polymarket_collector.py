"""The Polymarket collector: history exists only if we save it as it happens. Each poll appends
new trades to an hourly raw file and reports any gap (trades we could not reach). The v1 feed
(/trades, retired 2026-10-24) serves one page; v2 (/v2/trades, the default) is walked with a
cursor, and its rows are renamed to v1's keys so the raw files, the loader and dbt agree."""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from gridcase.ingest.polymarket import V2_TO_V1, CollectorState, poll_once, v2_to_v1
from gridcase.ingest.polymarket_load import COLUMNS, load_raw
from gridcase.store.db import connect

ROOT = Path(__file__).resolve().parents[2]
# One fill as both feeds served it, fetched live on 2026-10-15 (v1 /trades, v2 /v2/trades).
SAME_TRADE = json.loads((ROOT / "tests/fixtures/polymarket/v1_v2_same_trade.json").read_text())


def trade(
    ts: int, tx: str, asset: str = "a1", size: float = 10.0, price: float = 0.5
) -> dict[str, Any]:
    return {
        "proxyWallet": "0xw",
        "side": "BUY",
        "asset": asset,
        "conditionId": "0xc",
        "size": size,
        "price": price,
        "timestamp": ts,
        "title": "Will X?",
        "slug": "will-x",
        "eventSlug": "x",
        "outcome": "Yes",
        "transactionHash": tx,
    }


def client_serving(pages: list[list[dict[str, Any]]]) -> httpx.Client:
    calls = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/trades"
        return httpx.Response(200, json=next(calls))

    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://data-api.polymarket.com"
    )


def read_raw(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*.jsonl.gz")):
        with gzip.open(p, "rt") as f:
            out += [json.loads(line) for line in f]
    return out


def test_new_trades_are_appended_once_to_their_hour_file(tmp_path: Path) -> None:
    # newest first, as the API returns them; 1790711000 = 2026-09-29T19:43:20Z
    first = [trade(1790711010, "t3"), trade(1790711005, "t2"), trade(1790711000, "t1")]
    second = [trade(1790711020, "t4"), *first]
    state = CollectorState()
    c = client_serving([first, second])
    assert poll_once(c, tmp_path, state, api="v1").new == 3
    r = poll_once(c, tmp_path, state, api="v1")
    assert r.new == 1 and r.gap is False
    rows = read_raw(tmp_path)
    assert [x["transactionHash"] for x in rows] == ["t1", "t2", "t3", "t4"]
    assert all("collected_at" in x for x in rows)
    assert (tmp_path / "trades" / "2026" / "09" / "29" / "19.jsonl.gz").exists()


def test_a_poll_that_cannot_reach_back_to_the_last_seen_trade_reports_a_gap(tmp_path: Path) -> None:
    state = CollectorState()
    c = client_serving(
        [[trade(1790711000, "t1")], [trade(1790711900, "t9"), trade(1790711800, "t8")]]
    )
    poll_once(c, tmp_path, state, api="v1")
    r = poll_once(c, tmp_path, state, api="v1")
    assert r.new == 2 and r.gap is True


def test_trades_sharing_a_transaction_are_kept_when_they_differ(tmp_path: Path) -> None:
    """One transaction can fill several outcomes; identity is (tx, asset, side, size, price)."""
    state = CollectorState()
    page = [trade(1790711000, "t1", asset="a1"), trade(1790711000, "t1", asset="a2")]
    c = client_serving([page, page])
    assert poll_once(c, tmp_path, state, api="v1").new == 2
    assert poll_once(c, tmp_path, state, api="v1").new == 0


def test_a_failed_request_is_reported_not_raised(tmp_path: Path) -> None:
    c = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
        base_url="https://data-api.polymarket.com",
    )
    r = poll_once(c, tmp_path, CollectorState(), api="v1")
    assert r.new == 0 and r.error is not None


def test_a_gap_is_written_down_for_the_loader(tmp_path: Path) -> None:
    state = CollectorState()
    c = client_serving(
        [[trade(1790711000, "t1")], [trade(1790711900, "t9"), trade(1790711800, "t8")]]
    )
    poll_once(c, tmp_path, state, api="v1")
    poll_once(c, tmp_path, state, api="v1")
    gap = json.loads((tmp_path / "gaps.jsonl").read_text().splitlines()[0])
    assert (gap["from_ts"], gap["to_ts"]) == (1790711000, 1790711800)


# ------------------------------------------------------------------ v2 (/v2/trades)


def v2(t: dict[str, Any]) -> dict[str, Any]:
    """A v1-shaped test trade as /v2/trades serves it."""
    back = {v: k for k, v in V2_TO_V1.items()}
    return {back.get(k, k): v for k, v in t.items()}


class V2Feed:
    """A /v2/trades stand-in: `trades` (v1 shape, newest first) served `page` at a time with a
    keyset-style cursor. Tests prepend to `trades` between polls; `requests` logs each call."""

    def __init__(self, trades: list[dict[str, Any]], page: int = 2) -> None:
        self.trades = trades
        self.page = page
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v2/trades"
            assert request.url.params["taker_only"] == "true"
            self.requests.append(request)
            # the cursor names the last trade served, like a keyset cursor
            after = request.url.params.get("cursor")
            keys = [t["transactionHash"] for t in self.trades]
            start = keys.index(after) + 1 if after else 0
            rows = self.trades[start : start + self.page]
            more = start + self.page < len(self.trades)
            nxt = rows[-1]["transactionHash"] if rows and more else None
            body = {
                "data": [v2(t) for t in rows],
                "pagination": {"limit": self.page, "has_more": more, "next_cursor": nxt},
            }
            return httpx.Response(200, json=body)

        return httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://data-api.polymarket.com"
        )


def test_a_v2_row_maps_to_the_v1_row_of_the_same_trade() -> None:
    assert v2_to_v1(SAME_TRADE["v2"]) == SAME_TRADE["v1"]
    assert list(v2_to_v1(SAME_TRADE["v2"])) == list(SAME_TRADE["v1"])  # same key order too


def test_v2_mapping_renames_keys_only_and_keeps_unknown_fields() -> None:
    row = {"transaction_hash": "0xt", "token_id": "1", "size": 1.5, "new_field": "x"}
    assert v2_to_v1(row) == {"transactionHash": "0xt", "asset": "1", "size": 1.5, "new_field": "x"}


def test_v2_is_the_default_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gridcase.config.TRADES_API", "v2")
    feed = V2Feed([trade(1790711010, "t2"), trade(1790711000, "t1")])
    assert poll_once(feed.client(), tmp_path, CollectorState()).new == 2


def test_the_v1_switch_polls_the_old_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gridcase.config.TRADES_API", "v1")
    c = client_serving([[trade(1790711000, "t1")]])  # asserts the v1 path /trades
    assert poll_once(c, tmp_path, CollectorState()).new == 1


def test_the_first_v2_poll_takes_one_page(tmp_path: Path) -> None:
    feed = V2Feed([trade(1790711000 - i, f"t{i}") for i in range(10)], page=3)
    r = poll_once(feed.client(), tmp_path, CollectorState(), api="v2")
    assert (r.new, r.pages, r.gap) == (3, 1, False)


def test_the_v2_walk_pages_back_until_it_passes_the_last_saved_trade(tmp_path: Path) -> None:
    feed = V2Feed([trade(1790711000, "t1"), trade(1790710990, "t0")], page=2)
    state = CollectorState()
    poll_once(feed.client(), tmp_path, state, api="v2")
    assert state.last_ts == 1790711000
    # seven new trades arrive, one in the same second as the last saved one
    feed.trades = [trade(1790711100 - i, f"n{i}") for i in range(6)] + [
        trade(1790711000, "same-second"),
        *feed.trades,
    ]
    r = poll_once(feed.client(), tmp_path, state, api="v2")
    # pages: n0 n1 | n2 n3 | n4 n5 | same-second t1 | t0 (older than last_ts: stop)
    assert (r.new, r.gap, r.pages) == (7, False, 5)
    assert len(feed.requests) == 1 + 5
    assert "cursor" not in feed.requests[1].url.params
    assert feed.requests[2].url.params["cursor"] == "n1"
    hashes = [x["transactionHash"] for x in read_raw(tmp_path)]
    assert sorted(hashes) == sorted(["t1", "t0", "same-second", *[f"n{i}" for i in range(6)]])
    assert len(hashes) == len(set(hashes))
    assert not (tmp_path / "gaps.jsonl").exists()


def test_v2_dedupes_across_polls(tmp_path: Path) -> None:
    feed = V2Feed([trade(1790711010, "t3"), trade(1790711005, "t2"), trade(1790711000, "t1")], 2)
    state = CollectorState()
    c = feed.client()
    assert poll_once(c, tmp_path, state, api="v2").new == 2  # first poll: one page
    feed.trades = [trade(1790711020, "t4"), *feed.trades]
    # pages t4 t3 | t2 t1: t4 and t1 are new, t3 and t2 already saved
    assert poll_once(c, tmp_path, state, api="v2").new == 2
    assert poll_once(c, tmp_path, state, api="v2").new == 0
    rows = read_raw(tmp_path)
    assert sorted(x["transactionHash"] for x in rows) == ["t1", "t2", "t3", "t4"]


def test_the_v2_page_cap_records_a_gap(tmp_path: Path) -> None:
    feed = V2Feed([trade(1790711000, "t1")], page=2)
    state = CollectorState()
    poll_once(feed.client(), tmp_path, state, api="v2")
    feed.trades = [trade(1790711900 - i, f"n{i}") for i in range(10)] + feed.trades
    r = poll_once(feed.client(), tmp_path, state, api="v2", max_pages=3)
    assert (r.new, r.gap, r.pages) == (6, True, 3)
    gap = json.loads((tmp_path / "gaps.jsonl").read_text().splitlines()[0])
    assert (gap["from_ts"], gap["to_ts"]) == (1790711000, 1790711900 - 5)


def test_a_v2_feed_that_ends_before_the_last_saved_trade_is_not_a_gap(tmp_path: Path) -> None:
    feed = V2Feed([trade(1790711000, "t1")], page=2)
    state = CollectorState()
    poll_once(feed.client(), tmp_path, state, api="v2")
    feed.trades = [trade(1790711900, "n1"), trade(1790711800, "n0")]  # nothing older served
    r = poll_once(feed.client(), tmp_path, state, api="v2")
    assert (r.new, r.gap) == (2, False)


def test_a_v2_walk_that_fails_midway_saves_nothing_and_retries(tmp_path: Path) -> None:
    trades = [trade(1790711100 - i, f"n{i}") for i in range(4)] + [trade(1790711000, "t1")]
    calls = {"n": 0}
    good = V2Feed(trades, page=2).client()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(503)
        return good.get(request.url.path, params=request.url.params)

    c = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://data-api.polymarket.com"
    )
    state = CollectorState(last_ts=1790711000)
    r = poll_once(c, tmp_path, state, api="v2")
    assert r.error is not None and r.new == 0 and state.last_ts == 1790711000
    assert read_raw(tmp_path) == []
    assert poll_once(c, tmp_path, state, api="v2").new == 5


# ------------------------------------------------------------------ v1/v2 parity downstream


def _dbt_columns() -> str:
    macro = (ROOT / "dbt/macros/raw_trades.sql").read_text()
    m = re.search(r"columns = (\{.*?\})\n", macro, re.S)
    assert m
    return " ".join(m.group(1).split())


def _dbt_trade_ids(path: Path) -> list[str]:
    """trade_id as dbt's stg_polymarket__trades computes it, over the macro's column spec."""
    rows = duckdb.sql(
        f"""
        SELECT md5(concat_ws('|', transactionHash, asset, side, cast(size as varchar),
                             cast(price as varchar)))
        FROM read_json('{path}', format = 'newline_delimited', compression = 'gzip',
                       columns = {_dbt_columns()})
        """
    ).fetchall()
    return [r[0] for r in rows]


def test_a_v2_collected_trade_gets_the_same_trade_id_as_its_v1_copy(tmp_path: Path) -> None:
    one = SAME_TRADE["v1"]
    v1_root, v2_root = tmp_path / "v1", tmp_path / "v2"
    poll_once(client_serving([[one]]), v1_root, CollectorState(), api="v1")
    feed = V2Feed([one])
    poll_once(feed.client(), v2_root, CollectorState(), api="v2")
    (f1,) = v1_root.rglob("*.jsonl.gz")
    (f2,) = v2_root.rglob("*.jsonl.gz")
    assert f1.relative_to(v1_root) == f2.relative_to(v2_root)  # same hour file
    strip = [{k: v for k, v in r.items() if k != "collected_at"} for r in (read_raw(v1_root))]
    assert strip == [{k: v for k, v in r.items() if k != "collected_at"} for r in read_raw(v2_root)]

    # dbt and the app's loader read the same columns and agree on the id
    assert " ".join(COLUMNS.split()) == _dbt_columns()
    (dbt_id,) = _dbt_trade_ids(f2)
    assert _dbt_trade_ids(f1) == [dbt_id]
    ids = []
    for root in (v1_root, v2_root):
        conn = connect(tmp_path / f"{root.name}.duckdb")
        assert load_raw(conn, root).new == 1
        ids.append(
            conn.execute(
                "SELECT trade_id, wallet, condition_id, asset, event_slug, tx_hash FROM trades"
            ).fetchall()
        )
        conn.close()
    assert ids[0] == ids[1]
    assert ids[0][0][0] == dbt_id
    assert ids[0][0][1:] == (
        one["proxyWallet"],
        one["conditionId"],
        one["asset"],
        one["eventSlug"],
        one["transactionHash"],
    )


def test_v2_since_returns_every_trade_from_a_start_and_says_if_it_got_there() -> None:
    from gridcase.ingest.polymarket import v2_since

    feed = V2Feed([trade(1790711100 - 10 * i, f"t{i}") for i in range(10)], page=3)
    rows, complete = v2_since(feed.client(), 1790711050, max_pages=10)
    assert [r["transactionHash"] for r in rows] == [f"t{i}" for i in range(6)] and complete
    rows, complete = v2_since(feed.client(), 1790711000, max_pages=2)
    assert len(rows) == 6 and not complete
