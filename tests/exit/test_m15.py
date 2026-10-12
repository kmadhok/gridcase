"""M15 exit: backfill, point-in-time history tools, a frozen eval set, and RL training.

Plan: docs/plans/2026-10-12_rl_backfill_eval_train.md. Each phase's contract is a section
below, written before its code.

Phase A, the spend ledger. Every paid model call (frontier evals, small-model evals, training)
reserves its cost first. The ledger is append-only JSONL, so it survives restarts; it refuses any
reservation that would pass the phase's allowance or the global cap ($150)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from gridcase.backfill.build import build
from gridcase.backfill.census import market_row
from gridcase.backfill.client import Client
from gridcase.backfill.fetch import clob_prices, life, parse_ts, settled_token0
from gridcase.backfill.jobs import MARKET_COLUMNS, PRICE_COLUMNS, TRADE_COLUMNS, write_parquet
from gridcase.ingest.polymarket_load import categorize
from gridcase.rl.spend import ALLOWANCES, GLOBAL_CAP_USD, OverBudget, Spend

# ---------- phase A: spend ledger ----------


def test_allowances_sum_to_the_cap() -> None:
    assert GLOBAL_CAP_USD == 150.0
    assert sum(ALLOWANCES.values()) == pytest.approx(GLOBAL_CAP_USD)
    assert set(ALLOWANCES) == {"frontier_eval", "small_eval", "train", "reserve"}


def test_ledger_records_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    s = Spend(path)
    s.record("small_eval", "qwen", 1000, 200, 0.25)
    s.record("frontier_eval", "claude-sonnet-5-5", 5000, 900, 1.5)
    again = Spend(path)
    assert again.spent() == pytest.approx(1.75)
    assert again.spent("small_eval") == pytest.approx(0.25)
    assert len(path.read_text().splitlines()) == 2


def test_reserve_refuses_past_phase_allowance(tmp_path: Path) -> None:
    s = Spend(tmp_path / "spend.jsonl")
    s.record("small_eval", "qwen", 0, 0, ALLOWANCES["small_eval"] - 1.0)
    s.reserve("small_eval", 0.99)  # fits
    with pytest.raises(OverBudget):
        s.reserve("small_eval", 1.01)


def test_reserve_refuses_past_global_cap_even_with_phase_room(tmp_path: Path) -> None:
    s = Spend(tmp_path / "spend.jsonl", cap=10.0)
    s.record("train", "qwen", 0, 0, 9.5)
    with pytest.raises(OverBudget):
        s.reserve("frontier_eval", 1.0)


def test_unknown_phase_is_refused(tmp_path: Path) -> None:
    s = Spend(tmp_path / "spend.jsonl")
    with pytest.raises(KeyError):
        s.reserve("party", 1.0)


# ---------- phase B: backfill ----------
#
# The client caches every answer (a 400 included) and retries 429s; prices are fetched in
# 14-day chunks (CLOB refuses longer windows); both resolution formats parse; a market's life
# falls back to its close when Gamma's end date precedes its start; the census keeps resolved
# binary markets only and drops recurring "Up or Down" series; categories come from the live
# collector's categorize(); build() writes hist_markets without any outcome field.

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "backfill"


def _load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def _mock_client(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]) -> Client:
    return Client(tmp_path / "cache", rate_per_s=1000, transport=httpx.MockTransport(handler))


def test_client_caches_answers_and_400s_and_retries_429(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.params.get("x") == "bad":
            return httpx.Response(400, text='{"error":"interval is too long"}')
        if len(calls) == 2:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": req.url.params.get("x")})

    c = _mock_client(tmp_path, handler)
    assert c.get("https://x.test/a", {"x": "bad"})["_error"] == 400
    assert c.get("https://x.test/a", {"x": "1"}) == {"ok": "1"}  # one 429, then 200
    assert c.retries == 1
    n = len(calls)
    assert c.get("https://x.test/a", {"x": "bad"})["_error"] == 400  # from the cache
    assert c.get("https://x.test/a", {"x": "1"}) == {"ok": "1"}
    assert len(calls) == n and c.cache_hits == 2


def test_prices_are_fetched_in_chunks_under_15_days(tmp_path: Path) -> None:
    windows: list[tuple[int, int]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        lo, hi = int(req.url.params["startTs"]), int(req.url.params["endTs"])
        windows.append((lo, hi))
        return httpx.Response(200, json={"history": [{"t": lo, "p": 0.5}, {"t": hi, "p": 0.6}]})

    start = 1_780_000_000
    pts = clob_prices(_mock_client(tmp_path, handler), "tok", start, start + 40 * 86400, 10)
    assert len(windows) == 3
    assert all(hi - lo <= 15 * 86400 for lo, hi in windows)
    assert windows[0][0] == start and windows[-1][1] == start + 40 * 86400
    assert [t for t, _ in pts] == sorted({t for w in windows for t in w})  # deduplicated


def test_real_prices_fixture_parses(tmp_path: Path) -> None:
    fx = _load("prices_history.json")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fx["body"])

    p = fx["params"]
    pts = clob_prices(
        _mock_client(tmp_path, handler), p["market"], int(p["startTs"]), int(p["endTs"]), 10
    )
    assert len(pts) == len(fx["body"]["history"]) and all(0 <= v <= 1 for _, v in pts)


def test_both_resolution_formats_parse() -> None:
    uma = _load("resolution_uma.json")["data"][0]
    pay = _load("resolution_payouts.json")["data"][0]
    assert settled_token0(uma) in (0.0, 0.5, 1.0)
    assert settled_token0(pay) in (0.0, 0.5, 1.0)
    assert settled_token0({"price": "1000000000000000000"}) == 1.0
    assert settled_token0({"payouts": [0, 1_000_000]}) == 0.0
    assert settled_token0({}) is None


def test_life_falls_back_to_close_when_end_precedes_start() -> None:
    assert life(100, 50, 400) == (100, 400)  # end before start: use the close
    assert life(100, 300, 400) == (100, 300)  # the earlier of end and close
    assert life(100, None, None) is None and life(None, 300, 400) is None
    assert parse_ts("2026-04-21 00:28:28+00") == parse_ts("2026-04-21T00:28:28Z")


def test_census_keeps_resolved_binary_non_recurring_markets() -> None:
    page = _load("gamma_keyset.json")["markets"]
    rows = [market_row(m) for m in page]
    kept = [r for r in rows if r is not None]
    assert kept, "the fixture has ordinary resolved markets"
    for m, r in zip(page, rows, strict=True):
        if "up or down" in (m.get("question") or "").lower():
            assert r is None
        if sorted(json.loads(m.get("outcomePrices") or "[]")) != ["0", "1"]:
            assert r is None
    for r in kept:
        assert r["winner_index"] in (0, 1) and r["token0"] and r["token1"]
        tags = [t for t in r["tags"].split("|") if t]
        assert r["category"] == categorize(tags, f"{r['event_title'] or ''} {r['question']}")


def _synthetic_backfill(src: Path) -> None:
    t0 = 1_780_000_000
    markets = [
        {
            "condition_id": f"0xc{i}",
            "market_id": str(i),
            "question": f"Will {i} happen?",
            "slug": f"m{i}",
            "event_id": "e1",
            "event_slug": "e1",
            "event_title": "Event",
            "tags": "Politics",
            "category": "Politics",
            "volume": 1e6,
            "start_ts": t0,
            "end_ts": t0 + 10 * 86400,
            "closed_ts": t0 + 10 * 86400,
            "neg_risk": False,
            "token0": f"t{i}a",
            "token1": f"t{i}b",
            "outcome0": "Yes",
            "outcome1": "No",
            "winner_index": i % 2,
            "uma_status": "resolved",
        }  # fmt: skip
        for i in range(2)
    ]
    write_parquet(markets, MARKET_COLUMNS, src / "markets.parquet")
    prices = [
        {"condition_id": f"0xc{i}", "ts": t0 + k * 600, "p": 0.4 + 0.01 * i}
        for i in range(2)
        for k in range(10)
    ]
    write_parquet(prices, PRICE_COLUMNS, src / "prices" / "part-00000.parquet")
    trades = [
        {
            "condition_id": "0xc0",
            "token_id": "t0a",
            "proxy_wallet": f"0xw{k}",
            "side": "BUY",
            "size": 100.0,
            "price": 0.4,
            "timestamp": t0 + k * 60,
            "outcome": "Yes",
            "outcome_index": 0,
            "transaction_hash": f"0xh{k}",
            "title": "Will 0 happen?",
            "slug": "m0",
            "event_slug": "e1",
        }  # fmt: skip
        for k in range(5)
    ]
    write_parquet(trades, TRADE_COLUMNS, src / "trades" / "part-00000.parquet")


def test_build_writes_tables_and_keeps_outcomes_out_of_hist_markets(tmp_path: Path) -> None:
    src = tmp_path / "backfill"
    _synthetic_backfill(src)
    counts = build(src, tmp_path / "backfill.duckdb")
    assert counts == {
        "hist_markets": 2, "hist_outcomes": 2, "hist_prices": 20, "trades": 5, "markets": 1
    }  # fmt: skip
    con = duckdb.connect(str(tmp_path / "backfill.duckdb"), read_only=True)
    cols = {r[0] for r in con.execute("DESCRIBE hist_markets").fetchall()}
    assert not cols & {"winner_index", "uma_status", "y", "settled_token0", "outcome_prices"}
    assert con.execute("SELECT y FROM hist_outcomes ORDER BY condition_id").fetchall() == [
        (1.0,), (0.0,)
    ]  # fmt: skip
    # the live store's macro works on backfilled trades (collected_at = ts)
    n = con.execute("SELECT count(*) FROM trades_as_of(TIMESTAMP '2026-05-28 20:30:00')")
    assert (n.fetchone() or [0])[0] > 0
