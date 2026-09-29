"""The Polymarket collector: the public trades feed only reaches back ~10,000 trades (about
20 minutes), so history exists only if we save it as it happens. Each poll appends new trades
to an hourly raw file and reports any gap (trades we could not reach)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx

from gridcase.ingest.polymarket import CollectorState, poll_once


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
    assert poll_once(c, tmp_path, state).new == 3
    r = poll_once(c, tmp_path, state)
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
    poll_once(c, tmp_path, state)
    r = poll_once(c, tmp_path, state)
    assert r.new == 2 and r.gap is True


def test_trades_sharing_a_transaction_are_kept_when_they_differ(tmp_path: Path) -> None:
    """One transaction can fill several outcomes; identity is (tx, asset, side, size, price)."""
    state = CollectorState()
    page = [trade(1790711000, "t1", asset="a1"), trade(1790711000, "t1", asset="a2")]
    c = client_serving([page, page])
    assert poll_once(c, tmp_path, state).new == 2
    assert poll_once(c, tmp_path, state).new == 0


def test_a_failed_request_is_reported_not_raised(tmp_path: Path) -> None:
    c = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
        base_url="https://data-api.polymarket.com",
    )
    r = poll_once(c, tmp_path, CollectorState())
    assert r.new == 0 and r.error is not None
