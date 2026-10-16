"""M15 exit: backfill, point-in-time history tools, a frozen eval set, and RL training.

Plan: docs/plans/2026-10-12_rl_backfill_eval_train.md. Each phase's contract is a section
below, written before its code.

Phase A, the spend ledger. Every paid model call (frontier evals, small-model evals, training)
reserves its cost first. The ledger is append-only JSONL, so it survives restarts; it refuses any
reservation that would pass the phase's allowance or the global cap ($150)."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gridcase.backfill.build import build
from gridcase.backfill.census import market_row
from gridcase.backfill.client import Client
from gridcase.backfill.fetch import clob_prices, life, parse_ts, settled_token0
from gridcase.backfill.jobs import (
    MARKET_COLUMNS,
    PRICE_COLUMNS,
    STATUS_COLUMNS,
    TRADE_COLUMNS,
    write_parquet,
)
from gridcase.ingest.polymarket_load import categorize
from gridcase.rl.agent import (
    AGENT_TOOLS,
    MAX_TOOL_CALLS,
    ScriptedAgent,
    ToolCall,
    Turn,
    prompt_for,
    run_episode,
    tool_specs,
)
from gridcase.rl.harness import market_baseline
from gridcase.rl.models import AnthropicAgent, TinkerAgent
from gridcase.rl.reward import brier, ece, log_loss, paired_bootstrap, reward_for
from gridcase.rl.spend import ALLOWANCES, GLOBAL_CAP_USD, OverBudget, Spend
from gridcase.rl.tasks import (
    Answer,
    ClusterItem,
    SpikeParams,
    Splits,
    Task,
    answers_sha,
    assign_split,
    detect_spikes,
    drop_cross_split_clusters,
    freeze,
    load_answers,
    load_manifest,
    news_clusters,
    sample_split,
)
from gridcase.rl.train import (
    episode_datums,
    group_advantages,
    hacking_signal,
    make_batch,
    pick_checkpoint,
    should_stop,
    steps_affordable,
    trainable,
)
from gridcase.store.db import connect as gc_connect
from gridcase.tools.base import ToolError, run_tool
from gridcase.tools.registry import ALL_INPUTS, HIST_INPUTS, TOOL_NAMES

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


def test_client_cache_is_gzipped_and_still_reads_plain_json(tmp_path: Path) -> None:
    c = _mock_client(tmp_path, lambda req: httpx.Response(200, json={"ok": 1}))
    assert c.get("https://x.test/a", {"x": "1"}) == {"ok": 1}
    assert [p.suffixes for p in (tmp_path / "cache").rglob("*.*")] == [[".json", ".gz"]]
    # an entry from the older, uncompressed cache is served without a request
    legacy = c._path("https://x.test/b", {})
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"url": "https://x.test/b", "params": {}, "body": [7]}))
    offline = Client(tmp_path / "cache", offline=True)
    assert offline.get("https://x.test/a", {"x": "1"}) == {"ok": 1}
    assert offline.get("https://x.test/b") == [7]


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


# ---------- phase C: point-in-time history tools ----------
#
# hist.market / hist.series / hist.move / hist.siblings / hist.related read backfill.duckdb
# through prices_as_of(t) and hist_markets_as_of(t). Contract: a pure function of
# (inputs, as_of). Changing anything after as_of (later prices, markets that start later, a
# close that happens later) leaves every output byte-identical; no output ever carries the
# outcome, the lifetime volume or a future close time.

T0 = 1_780_000_000 // 3600 * 3600  # an hour boundary
HIST_TOOLS = [
    "hist.market", "hist.series", "hist.move", "hist.siblings", "hist.related", "hist.flow"
]  # fmt: skip


def _hist_markets(extra_late: bool = False, close_late: int = 9) -> list[dict[str, Any]]:
    def m(i: int, event: str, tags: str, start: int, closed: int) -> dict[str, Any]:
        return {
            "condition_id": f"0xm{i}", "market_id": str(i), "question": f"Q{i}?",
            "slug": f"m{i}", "event_id": event, "event_slug": event,
            "event_title": f"Event {event}", "tags": tags, "category": "Politics",
            "volume": 1e6 * (i + 1), "start_ts": start, "end_ts": T0 + 20 * 86400,
            "closed_ts": closed, "neg_risk": False, "token0": f"t{i}", "token1": f"u{i}",
            "outcome0": "Yes", "outcome1": "No", "winner_index": i % 2,
            "uma_status": "resolved",
        }  # fmt: skip

    out = [
        m(0, "e1", "Politics|Iran", T0, T0 + close_late * 86400),
        m(1, "e1", "Politics|Iran", T0, T0 + close_late * 86400),
        m(2, "e2", "Politics|Iran", T0, T0 + 12 * 86400),
        m(3, "e3", "Politics|Sports", T0, T0 + 12 * 86400),
    ]
    if extra_late:  # starts after every as_of we test
        out.append(m(4, "e1", "Politics|Iran", T0 + 9 * 86400, T0 + 12 * 86400))
    return out


def _hist_prices(seed: int, scramble_after: int | None = None) -> list[dict[str, Any]]:
    import random

    rows = []
    for i in range(5):
        rng = random.Random(seed * 10 + i)
        scr = random.Random(seed * 99 + i)
        p = 0.5
        for k in range(0, 10 * 144):  # 10 days of 10-minute points
            ts = T0 + k * 600
            p = min(0.98, max(0.02, p + rng.uniform(-0.03, 0.03)))
            val = scr.uniform(0.02, 0.98) if scramble_after and ts > scramble_after else p
            rows.append({"condition_id": f"0xm{i}", "ts": ts, "p": round(val, 4)})
    return rows


def _hist_trades(seed: int, scramble_after: int | None = None) -> list[dict[str, Any]]:
    import random

    rng, scr = random.Random(seed), random.Random(seed + 1)
    rows = []
    for k in range(10 * 24 * 4):  # every 15 minutes for 10 days, market 0 only
        ts = T0 + k * 900
        size = rng.choice([5.0, 50.0, 500.0, 5000.0])
        if scramble_after and ts > scramble_after:
            size = scr.choice([1.0, 9999.0])
        rows.append({
            "condition_id": "0xm0", "token_id": "t0", "proxy_wallet": f"0xw{k % 37}",
            "side": "BUY" if k % 3 else "SELL", "size": size, "price": 0.5, "timestamp": ts,
            "outcome": "Yes" if k % 2 else "No", "outcome_index": k % 2,
            "transaction_hash": f"0xh{k}", "title": "Q0?", "slug": "m0", "event_slug": "e1",
        })  # fmt: skip
    return rows


def _hist_db(path: Path, seed: int, scramble_after: int | None = None, late: bool = False) -> Path:
    src = path / "src"
    close_late = 9 if not late else 8  # a different (future) close time in the mutated copy
    write_parquet(_hist_markets(late, close_late), MARKET_COLUMNS, src / "markets.parquet")
    write_parquet(
        _hist_prices(seed, scramble_after), PRICE_COLUMNS, src / "prices" / "part-00000.parquet"
    )
    write_parquet(
        _hist_trades(seed, scramble_after), TRADE_COLUMNS, src / "trades" / "part-00000.parquet"
    )
    write_parquet(
        [{"condition_id": "0xm0", "needed_from": T0, "complete": True}],
        STATUS_COLUMNS,
        src / "trades_status.parquet",
    )
    db = path / "backfill.duckdb"
    build(src, db)
    return db


def _hist_inputs(name: str) -> dict[str, Any]:
    base: dict[str, Any] = {"condition_id": "0xm0"}
    if name == "hist.series":
        return {**base, "hours": 24, "grain": "1h"}
    if name == "hist.siblings":
        return {**base, "hours": 6}
    if name == "hist.related":
        return {**base, "hours": 6, "min_move_pts": 1}
    if name == "hist.flow":
        return {**base, "hours": 24}
    return base


def _outputs(db: Path, as_of: datetime) -> dict[str, Any]:
    con = gc_connect(db, read_only=True)
    try:
        return {n: run_tool(con, n, _hist_inputs(n), as_of).output for n in HIST_TOOLS}
    finally:
        con.close()


@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(seed=st.integers(0, 10_000), hour=st.integers(80, 7 * 24))
def test_tools_never_see_after_as_of(
    tmp_path_factory: pytest.TempPathFactory, seed: int, hour: int
) -> None:
    as_of_ts = T0 + hour * 3600
    as_of = datetime.fromtimestamp(as_of_ts, UTC).replace(tzinfo=None)
    a = _hist_db(tmp_path_factory.mktemp("a"), seed)
    b = _hist_db(tmp_path_factory.mktemp("b"), seed, scramble_after=as_of_ts, late=True)
    assert _outputs(a, as_of) == _outputs(b, as_of)


def test_outputs_carry_no_outcome_volume_or_future_close(tmp_path: Path) -> None:
    db = _hist_db(tmp_path, seed=3)
    as_of = datetime.fromtimestamp(T0 + 5 * 86400, UTC).replace(tzinfo=None)
    outs = _outputs(db, as_of)
    text = json.dumps(outs)
    for banned in ("winner", "uma_status", "settled", '"volume":', '"y"'):
        assert banned not in text, banned
    for m in _hist_markets():  # lifetime volume includes trading after as_of
        assert str(m["volume"]) not in text
    assert outs["hist.market"]["closed_at"] is None  # closes on day 9, after as_of
    assert outs["hist.market"]["question"] == "Q0?"
    assert {s["condition_id"] for s in outs["hist.siblings"]["siblings"]} == {"0xm1"}
    assert outs["hist.related"]["specific_tags"] == ["Iran"]  # "Politics" is on every event
    related = {r["condition_id"] for r in outs["hist.related"]["related"]}
    assert related <= {"0xm2"}  # shares the specific tag "Iran"; 0xm3 shares only "Politics"


def test_hist_tools_are_registered_but_not_offered_to_the_planner() -> None:
    assert set(HIST_TOOLS) <= set(HIST_INPUTS) <= set(ALL_INPUTS)
    assert not set(HIST_TOOLS) & set(TOOL_NAMES)


def test_hist_receipts_are_stable_and_series_is_capped(tmp_path: Path) -> None:
    db = _hist_db(tmp_path, seed=5)
    as_of = datetime.fromtimestamp(T0 + 6 * 86400, UTC).replace(tzinfo=None)
    con = gc_connect(db, read_only=True)
    r1 = run_tool(con, "hist.move", {"condition_id": "0xm0"}, as_of)
    r2 = run_tool(con, "hist.move", {"condition_id": "0xm0"}, as_of)
    assert r1.receipt_id == r2.receipt_id
    assert set(r1.output["change"]) == {"1h", "6h", "24h", "72h"}
    with pytest.raises(ToolError):
        run_tool(con, "hist.series", {"condition_id": "0xm0", "hours": 720, "grain": "10m"}, as_of)
    missing = run_tool(con, "hist.market", {"condition_id": "0xnope"}, as_of)
    assert missing.output == {"found": False}
    flow = run_tool(con, "hist.flow", {"condition_id": "0xm0", "hours": 24}, as_of)
    assert flow.output["trade_data"] is True
    assert flow.output["window"]["trades"] == 96  # one every 15 minutes
    assert any("trades_as_of" in q["sql"] for q in flow.sql)  # governed: the semantic layer
    assert sum(flow.output["volume_by_trade_size"].values()) == pytest.approx(
        flow.output["window"]["volume_usd"]
    )
    none = run_tool(con, "hist.flow", {"condition_id": "0xm1", "hours": 24}, as_of)
    assert none.output["trade_data"] is False
    con.close()


# ---------- phase D: tasks, splits, frozen eval set ----------
#
# Spikes (feasibility definitions, FEASIBILITY.md §3): |p(t) - p(t-6h)| >= 10 pts on a 10-minute
# grid, base in [0.05, 0.95], 72 h of trading after t, 24 h cooldown per market. spike_hold's
# answer is "retained >= 50% of the move at +72 h". resolve tasks ask at close - 24 h (and
# close - 7 d) when the price is in [0.05, 0.95], and use only markets whose Gamma and /v2
# outcomes agree. Splits are by as_of with purge gaps; a task whose label window passes its
# split's end is dropped, and so is any cluster that would span two splits. The manifest
# carries no answers; answers.sha256 pins them.

DAY = 86400


def _step_series(
    start: int, days: int, events: list[tuple[float, float]]
) -> list[tuple[int, float]]:
    """10-minute points; `events` = (day offset, new price) steps."""
    out, p = [], 0.30
    for k in range(days * 144):
        t = start + k * 600
        for d, newp in events:
            if abs(t - (start + d * DAY)) < 300:
                p = newp
        out.append((t, p))
    return out


def test_spike_detection_labels_cooldown() -> None:
    s = 1_780_000_200 // 600 * 600
    held = _step_series(s, 12, [(5.0, 0.45)])
    reverted = _step_series(s, 12, [(5.0, 0.45), (5.5, 0.31)])
    twice = _step_series(s, 12, [(5.0, 0.45), (5.5, 0.60)])  # second jump inside the cooldown
    close = s + 12 * DAY
    p = SpikeParams()
    (h,) = detect_spikes(held, close, p)
    assert h.move == pytest.approx(0.15) and h.retained72 == pytest.approx(1.0) and h.held
    (r,) = detect_spikes(reverted, close, p)
    assert not r.held and r.retained72 == pytest.approx(0.01 / 0.15)
    assert len(detect_spikes(twice, close, p)) == 1
    # no spike when the 72 h label window would pass the close
    assert detect_spikes(held, s + 7 * DAY, p) == []
    # no spike from an already-decided base
    decided = [(t, 0.02 if t < s + 5 * DAY else 0.2) for t, _ in held]
    assert detect_spikes(decided, close, p) == []


def test_news_clusters_join_same_event_and_shared_specific_tags() -> None:
    t = 1_780_000_000
    spikes = [
        ClusterItem(t, "e1", ("Iran", "Politics")),
        ClusterItem(t + 3600, "e1", ("Iran", "Politics")),  # same event, 1 h later
        ClusterItem(t + 1800, "e2", ("Iran", "Politics")),  # shares "Iran" within 2 h
        ClusterItem(t + 1800, "e3", ("Politics",)),  # shares only a generic tag
        ClusterItem(t + 9 * 3600, "e2", ("Iran",)),  # too late
    ]
    ids = news_clusters(spikes, generic={"Politics"})
    assert ids[0] == ids[1] == ids[2]
    assert len({ids[0], ids[3], ids[4]}) == 3


SPLITS = Splits(
    train_end=datetime(2026, 5, 8), val_start=datetime(2026, 5, 15),
    val_end=datetime(2026, 6, 15), eval_start=datetime(2026, 7, 1),
    eval_end=datetime(2026, 10, 11),
)  # fmt: skip


def test_split_assignment_with_purge() -> None:
    d = datetime
    assert assign_split(d(2026, 4, 1), d(2026, 4, 4), SPLITS) == "train"
    assert assign_split(d(2026, 5, 7), d(2026, 5, 10), SPLITS) is None  # label crosses train end
    assert assign_split(d(2026, 5, 10), d(2026, 5, 13), SPLITS) is None  # in the purge gap
    assert assign_split(d(2026, 5, 20), d(2026, 5, 23), SPLITS) == "validation"
    assert assign_split(d(2026, 6, 20), d(2026, 6, 23), SPLITS) is None
    assert assign_split(d(2026, 8, 1), d(2026, 8, 4), SPLITS) == "eval"
    assert assign_split(d(2026, 10, 9), d(2026, 10, 12), SPLITS) is None


def _task(
    i: int, split: str, cluster: str, cat: str = "Politics", kind: str = "spike_hold"
) -> Task:
    return Task(
        task_id=f"t{i}", kind=kind, condition_id=f"0x{i}", as_of=datetime(2026, 8, 1),  # type: ignore[arg-type]
        facts={}, cluster_id=cluster, split=split, category=cat,  # type: ignore[arg-type]
    )  # fmt: skip


def test_clusters_never_span_splits_and_sampling_respects_quotas() -> None:
    tasks = [_task(0, "train", "c1"), _task(1, "eval", "c1"), _task(2, "eval", "c2")]
    kept = drop_cross_split_clusters(tasks)
    assert [t.task_id for t in kept] == ["t2"]
    pool = [_task(i, "eval", f"c{i // 2}", "Sports" if i % 3 else "Politics") for i in range(60)]
    picked = sample_split(pool, n=10, per_cluster=1, sports_cap=0.4, seed=1)
    assert len(picked) == 10
    assert len({t.cluster_id for t in picked}) == 10
    assert sum(t.category == "Sports" for t in picked) <= 4
    assert picked == sample_split(pool, n=10, per_cluster=1, sports_cap=0.4, seed=1)  # stable


def test_manifest_has_no_answers_and_the_hash_pins_them(tmp_path: Path) -> None:
    tasks = [_task(i, "eval", f"c{i}") for i in range(3)]
    answers = {
        t.task_id: Answer(task_id=t.task_id, y=float(i % 2), detail={}) for i, t in enumerate(tasks)
    }
    sha = freeze(tasks, answers, tmp_path / "evals", tmp_path / "rl")
    lines = (tmp_path / "evals" / "manifest.jsonl").read_text().splitlines()
    assert len(lines) == 3
    for ln in lines:
        assert not {"y", "answer", "retained72", "winner"} & set(json.loads(ln))
    assert (tmp_path / "evals" / "answers.sha256").read_text().strip() == sha
    assert answers_sha(load_answers(tmp_path / "rl" / "answers.jsonl")) == sha
    assert load_manifest(tmp_path / "evals" / "manifest.jsonl") == tasks


# ---------- phase E: agent harness, reward, baselines ----------
#
# Every model gets the same system prompt, the same tools (hist.* plus submit_answer), a budget
# of 8 tool calls and a cap on tool-output size. Tool calls run through run_tool at the task's
# as_of. submit_answer's summary and claims pass explain/verifier.py (uncited numbers dropped).
# Reward = 1 - (p - y)^2 with p clipped to [0.01, 0.99]; -0.25 for no valid answer; a further
# -0.25 if the tool budget ran out or the verifier dropped more than half the claims.


def _resolve_task(as_of_ts: int, price: float) -> Task:
    return Task(
        task_id=f"resolve:0xm0:{as_of_ts}", kind="resolve", condition_id="0xm0",
        as_of=datetime.fromtimestamp(as_of_ts, UTC).replace(tzinfo=None),
        facts={"horizon_hours": 24, "price_now": price}, cluster_id="event:e1",
        split="eval", category="Politics",
    )  # fmt: skip


def test_reward_and_scores() -> None:
    assert reward_for(0.8, 1.0, malformed=False, penalised=False) == pytest.approx(0.96)
    assert reward_for(1.0, 0.0, malformed=False, penalised=False) == pytest.approx(1 - 0.99**2)
    assert reward_for(None, 1.0, malformed=True, penalised=False) == -0.25
    assert reward_for(0.5, 1.0, malformed=False, penalised=True) == pytest.approx(0.5)
    assert brier(0.7, 1.0) == pytest.approx(0.09)
    assert log_loss(0.0, 0.0) == pytest.approx(-math.log(0.99))
    assert ece([0.1, 0.1, 0.9, 0.9], [0.0, 0.0, 1.0, 1.0], bins=10) == pytest.approx(0.1)


def test_paired_bootstrap_over_clusters() -> None:
    a = {f"t{i}": 0.10 for i in range(40)}
    b = {f"t{i}": 0.20 for i in range(40)}
    cluster = {f"t{i}": f"c{i // 4}" for i in range(40)}
    mean, lo, hi = paired_bootstrap(a, b, cluster, draws=500, seed=1)
    assert (
        mean == pytest.approx(-0.10) and lo == pytest.approx(-0.10) and hi == pytest.approx(-0.10)
    )
    b2 = {k: (0.0 if i % 2 else 0.3) for i, k in enumerate(b)}  # noisy, mean 0.15
    mean2, lo2, hi2 = paired_bootstrap(a, b2, cluster, draws=500, seed=1)
    assert lo2 <= mean2 <= hi2 and mean2 == pytest.approx(-0.05)


def _turn(calls: list[tuple[str, dict[str, Any]]], usd: float = 0.0) -> Turn:
    return Turn(
        text="",
        calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)],
        usd=usd,
    )


def test_scripted_agent_reproduces_the_market_baseline(tmp_path: Path) -> None:
    db = _hist_db(tmp_path, seed=11)
    as_of_ts = T0 + 6 * 86400
    con = gc_connect(db, read_only=True)
    move = run_tool(con, "hist.move", {"condition_id": "0xm0"}, _resolve_task(as_of_ts, 0.5).as_of)
    task = _resolve_task(as_of_ts, move.output["price"])
    seen: dict[str, Any] = {}

    def policy(history: list[dict[str, Any]]) -> Turn:
        if len(history) == 1:
            return _turn([("hist_move", {"condition_id": "0xm0"})])
        result = json.loads(history[-1]["results"][0][1])
        seen.update(result)
        p = result["output"]["price"]
        return _turn([("submit_answer", {
            "probability": p, "summary": "The market price is the forecast.",
            "claims": [{"text": f"The price is {p}.", "receipt_ids": [result["receipt_id"]]},
                       {"text": "It will surely be 0.777.", "receipt_ids": []}],
        })])  # fmt: skip

    ep = run_episode(ScriptedAgent(policy), con, task)
    assert ep.probability == pytest.approx(market_baseline(task, {}))
    assert ep.tool_calls == 1 and not ep.malformed and not ep.budget_exceeded
    assert len(ep.claims) == 1 and len(ep.dropped) == 1  # the uncited number is dropped
    assert seen["receipt_id"] == move.receipt_id  # the same receipt, at the task's as_of
    con.close()


def test_tool_budget_and_malformed_answers(tmp_path: Path) -> None:
    db = _hist_db(tmp_path, seed=12)
    con = gc_connect(db, read_only=True)
    task = _resolve_task(T0 + 6 * 86400, 0.5)
    greedy = ScriptedAgent(lambda h: _turn([("hist_move", {"condition_id": "0xm0"})] * 3))
    ep = run_episode(greedy, con, task)
    assert ep.budget_exceeded and ep.malformed and ep.probability is None
    assert ep.tool_calls == MAX_TOOL_CALLS
    bad = ScriptedAgent(lambda h: _turn([("submit_answer", {"probability": 1.7, "summary": "x"})]))
    assert run_episode(bad, con, task).malformed
    errs = ScriptedAgent(
        lambda h: (
            _turn([("hist_move", {"condition_id": "0xm0", "bogus": 1})])
            if len(h) == 1
            else _turn([("submit_answer", {"probability": 0.4, "summary": "ok", "claims": []})])
        )
    )
    ep2 = run_episode(errs, con, task)
    assert ep2.probability == 0.4 and "error" in ep2.transcript[1]["results"][0][1]
    con.close()


def test_transcript_keeps_what_the_model_wrote(tmp_path: Path) -> None:
    """Diagnosis needs the model's own words: text and thinking go into the transcript."""
    db = _hist_db(tmp_path, seed=12)
    con = gc_connect(db, read_only=True)
    task = _resolve_task(T0 + 6 * 86400, 0.5)

    def policy(h: list[dict[str, Any]]) -> Turn:
        if len(h) == 1:
            t = _turn([("hist_move", {"condition_id": "0xm0"})])
            t.text, t.thinking = "Check the move first.", "The price rose; does it stick?"
            return t
        t = _turn([("submit_answer", {"probability": 0.3, "summary": "ok", "claims": []})])
        t.thinking = "Moves like this fade."
        return t

    ep = run_episode(ScriptedAgent(policy), con, task)
    assert ep.transcript[1]["text"] == "Check the move first."
    assert ep.transcript[1]["thinking"] == "The price rose; does it stick?"
    assert ep.transcript[-1] == {
        "role": "assistant",
        "thinking": "Moves like this fade.",
        "submit": {"probability": 0.3, "summary": "ok", "claims": []},
    }
    assert list(ep.transcript[-1]["submit"]) == ["probability", "summary", "claims"]
    con.close()


def test_spend_is_reserved_before_every_paid_call(tmp_path: Path) -> None:
    db = _hist_db(tmp_path, seed=13)
    con = gc_connect(db, read_only=True)
    task = _resolve_task(T0 + 6 * 86400, 0.5)
    spend = Spend(tmp_path / "spend.jsonl", cap=0.05)
    calls = {"n": 0}

    def policy(h: list[dict[str, Any]]) -> Turn:
        calls["n"] += 1
        return _turn([("hist_move", {"condition_id": "0xm0"})], usd=0.03)

    ep = run_episode(
        ScriptedAgent(policy, phase="small_eval", estimate=0.03), con, task, spend=spend
    )
    assert ep.error is not None and "small_eval" in ep.error
    assert calls["n"] == 1 and spend.spent() == pytest.approx(0.03)
    con.close()


def test_a_failed_model_call_ends_only_that_episode(tmp_path: Path) -> None:
    db = _hist_db(tmp_path, seed=14)
    con = gc_connect(db, read_only=True)
    task = _resolve_task(T0 + 6 * 86400, 0.5)

    def policy(h: list[dict[str, Any]]) -> Turn:
        raise RuntimeError("529 overloaded")

    ep = run_episode(ScriptedAgent(policy), con, task)
    assert ep.malformed and ep.probability is None
    assert ep.error is not None and "RuntimeError" in ep.error and "529" in ep.error
    con.close()


def test_errored_episodes_are_left_out_of_training() -> None:
    from gridcase.rl.agent import Episode

    ok = Episode(task_id="t", model="m")
    failed = Episode(task_id="t", model="m", error="APIError: 500")
    assert trainable([ok, failed]) == [ok]
    assert trainable([failed]) == []


def test_report_skips_the_trainers_own_files(tmp_path: Path, monkeypatch: Any) -> None:
    import gridcase.rl.cli as rl_cli
    from gridcase.rl.harness import summarize

    res = tmp_path / "results"
    res.mkdir()
    row = {
        "task_id": "t", "kind": "resolve", "category": "Sports", "cluster_id": "c", "y": 1.0,
        "p": 0.7, "market_p": 0.6, "brier": 0.09, "log_loss": 0.36, "market_brier": 0.16,
        "reward": 0.9,
    }  # fmt: skip
    (res / "market_eval.json").write_text(json.dumps({"summary": summarize([row])}))
    (res / "train_history.json").write_text(json.dumps([{"step": 10, "val_brier": 0.28}]))
    (res / "train_state.json").write_text(json.dumps({"steps": 39, "best": None}))
    monkeypatch.setattr(rl_cli, "M15", tmp_path)
    text = rl_cli.report()
    assert "| market_eval | 1 | 1 |" in text and "train_" not in text


def test_a_retry_replaces_only_the_errored_rows() -> None:
    from gridcase.rl.harness import errored_task_ids, merge_retry, summarize

    def row(tid: str, p: float | None, error: str | None) -> dict[str, Any]:
        return {
            "task_id": tid, "kind": "resolve", "category": "Sports", "cluster_id": tid,
            "y": 1.0, "p": p, "market_p": 0.5, "brier": None if p is None else (1 - p) ** 2,
            "log_loss": None if p is None else -math.log(p), "market_brier": 0.25,
            "reward": 0.0, "tool_calls": 1,
            "turns": 1, "claims": 0, "dropped": 0, "budget_exceeded": False,
            "malformed": p is None, "error": error, "usd": 0.01, "seconds": 1.0,
        }  # fmt: skip

    ok, failed = row("a", 0.8, None), row("b", None, "BadRequestError: credit balance")
    malformed_by_model = row("c", None, None)  # the model's own failure is not retried
    old = {"name": "frontier", "rows": [ok, failed, malformed_by_model]}
    old["summary"] = summarize(old["rows"])
    assert errored_task_ids(old) == {"b"}
    merged = merge_retry(old, {"rows": [row("b", 0.6, None)]})
    assert [r["task_id"] for r in merged["rows"]] == ["a", "b", "c"]
    assert merged["rows"][1]["p"] == 0.6 and merged["rows"][0] is ok
    assert merged["summary"]["all"]["answered"] == 2 and merged["name"] == "frontier"


def test_every_model_gets_the_same_tools() -> None:
    names = [t["name"] for t in tool_specs()]
    assert names == [*(n.replace(".", "_") for n in AGENT_TOOLS), "submit_answer"]
    assert set(AGENT_TOOLS) <= set(HIST_INPUTS)
    for t in tool_specs():
        assert t["input_schema"]["type"] == "object"


class _FakeAnthropic:
    """Messages API stand-in: replays scripted responses and records each request."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kw: Any) -> Any:
        self.requests.append(
            json.loads(json.dumps(kw, default=lambda o: getattr(o, "__dict__", str(o))))
        )
        return self.responses.pop(0)


def _block(**kw: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def test_anthropic_adapter_shapes_messages_and_costs(tmp_path: Path) -> None:
    from types import SimpleNamespace

    db = _hist_db(tmp_path, seed=14)
    con = gc_connect(db, read_only=True)
    task = _resolve_task(T0 + 6 * 86400, 0.5)
    usage = SimpleNamespace(input_tokens=1000, cache_creation_input_tokens=0,
                            cache_read_input_tokens=2000, output_tokens=100)  # fmt: skip
    client = _FakeAnthropic([
        SimpleNamespace(content=[_block(type="tool_use", id="tu1", name="hist_move",
                                        input={"condition_id": "0xm0"})],
                        usage=usage, stop_reason="tool_use"),
        SimpleNamespace(content=[_block(type="tool_use", id="tu2", name="submit_answer",
                                        input={"probability": 0.6, "summary": "ok", "claims": []})],
                        usage=usage, stop_reason="tool_use"),
    ])  # fmt: skip
    agent = AnthropicAgent(model="claude-sonnet-5-5", client=client)
    ep = run_episode(agent, con, task)
    assert ep.probability == 0.6 and ep.tool_calls == 1
    first, second = client.requests
    assert first["model"] == "claude-sonnet-5-5" and "fallbacks" not in first
    assert first["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in first["tools"]] == [t["name"] for t in tool_specs()]
    last_user = second["messages"][-1]
    assert last_user["role"] == "user" and last_user["content"][0]["type"] == "tool_result"
    assert last_user["content"][0]["tool_use_id"] == "tu1"
    per_turn = (1000 * 2.0 + 2000 * 2.0 * 0.1 + 100 * 10.0) / 1e6
    assert ep.usd == pytest.approx(2 * per_turn)
    con.close()


def test_tinker_adapter_renders_tools_and_parses_calls(tmp_path: Path) -> None:
    pytest.importorskip("tinker_cookbook")
    from types import SimpleNamespace

    try:
        from tinker_cookbook.tokenizer_utils import get_tokenizer

        tok = get_tokenizer("Qwen/Qwen3-8B")
    except Exception as e:  # no network and no cached tokenizer
        pytest.skip(f"Qwen3 tokenizer unavailable: {e}")
    replies = [
        '<tool_call>\n{"name": "hist_move", "arguments": {"condition_id": "0xm0"}}\n'
        "</tool_call><|im_end|>",
        '<tool_call>\n{"name": "submit_answer", "arguments": {"probability": 0.35, '
        '"summary": "Thin evidence.", "claims": []}}\n</tool_call><|im_end|>',
    ]
    prompts: list[str] = []

    class Sampler:
        def sample(self, prompt: Any, num_samples: int, sampling_params: Any) -> Any:
            prompts.append(tok.decode(prompt.to_ints()))
            ids = tok.encode(replies.pop(0), add_special_tokens=False)
            seq = SimpleNamespace(tokens=ids, logprobs=[-0.1] * len(ids))
            return SimpleNamespace(result=lambda: SimpleNamespace(sequences=[seq]))

    db = _hist_db(tmp_path, seed=15)
    con = gc_connect(db, read_only=True)
    agent = TinkerAgent(base_model="Qwen/Qwen3-8B", sampling_client=Sampler())
    ep = run_episode(agent, con, _resolve_task(T0 + 6 * 86400, 0.5))
    assert ep.probability == pytest.approx(0.35) and ep.tool_calls == 1
    assert '"name": "hist_flow"' in prompts[0] and "submit_answer" in prompts[0]  # same tools
    assert "<tool_response>" in prompts[1] and "receipt_id" in prompts[1]  # result fed back
    assert ep.raw_turns[0]["logprobs"] and ep.raw_turns[0]["prompt_tokens"]
    con.close()


def test_tinker_adapter_is_safe_across_eval_threads(tmp_path: Path) -> None:
    # The fast tokenizer raises "Already borrowed" when threads share it; the first real
    # base-model eval (16 workers) lost 599 of 600 episodes to that.
    pytest.importorskip("tinker_cookbook")
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    try:
        from tinker_cookbook.tokenizer_utils import get_tokenizer

        tok = get_tokenizer("Qwen/Qwen3-8B")
    except Exception as e:  # no network and no cached tokenizer
        pytest.skip(f"Qwen3 tokenizer unavailable: {e}")
    reply = tok.encode(
        '<tool_call>\n{"name": "submit_answer", "arguments": {"probability": 0.35, '
        '"summary": "Thin evidence.", "claims": []}}\n</tool_call><|im_end|>',
        add_special_tokens=False,
    )

    class Sampler:
        def sample(self, prompt: Any, num_samples: int, sampling_params: Any) -> Any:
            seq = SimpleNamespace(tokens=reply, logprobs=[-0.1] * len(reply))
            return SimpleNamespace(result=lambda: SimpleNamespace(sequences=[seq]))

    db = _hist_db(tmp_path, seed=16)
    con = gc_connect(db, read_only=True)
    agent = TinkerAgent(base_model="Qwen/Qwen3-8B", sampling_client=Sampler())
    task = _resolve_task(T0 + 6 * 86400, 0.5)

    def one(_: int) -> Any:
        cur = con.cursor()
        try:
            return run_episode(agent, cur, task)
        finally:
            cur.close()

    with ThreadPoolExecutor(16) as pool:
        eps = list(pool.map(one, range(64)))
    assert [e.error for e in eps if e.error] == []
    assert all(e.probability == pytest.approx(0.35) for e in eps)
    con.close()


# ---------- phase F: RL training (offline parts) ----------
#
# Group-relative advantages (reward minus the group mean; no spread -> nothing to learn), one
# Datum per sampled turn (prompt + sampled tokens shifted by one; logprobs and advantage on the
# sampled tokens, zeros on the prompt), the step count a pilot's cost affords, checkpoint choice
# on validation scores only, early stopping, and the reward-hacking guard.


def test_group_advantages() -> None:
    assert group_advantages([1.0, 0.0, 0.5, 0.5]) == pytest.approx([0.5, -0.5, 0.0, 0.0])
    assert group_advantages([0.7, 0.7, 0.7]) == [0.0, 0.0, 0.0]
    assert group_advantages([]) == []


def test_episode_datums_shift_and_mask() -> None:
    turns = [
        {"prompt_tokens": [1, 2, 3], "tokens": [4, 5], "logprobs": [-0.1, -0.2]},
        {"prompt_tokens": [1, 2, 3, 4, 5, 9], "tokens": [6], "logprobs": [-0.3]},
        {"prompt_tokens": [1], "tokens": [7], "logprobs": []},  # no logprobs: skipped
        None,
    ]
    d1, d2 = episode_datums(turns, 0.5)  # type: ignore[arg-type]
    assert d1.input_tokens == [1, 2, 3, 4] and d1.target_tokens == [2, 3, 4, 5]
    assert d1.logprobs == [0.0, 0.0, -0.1, -0.2]
    assert d1.advantages == [0.0, 0.0, 0.5, 0.5]
    assert len(d2.input_tokens) == len(d2.target_tokens) == len(d2.advantages) == 6
    assert d2.advantages[-1] == 0.5 and sum(d2.advantages[:-1]) == 0.0


def test_steps_affordable_from_the_pilot() -> None:
    assert steps_affordable(85.0, 2.0, 5.0, 150) == 40
    assert steps_affordable(85.0, 0.1, 5.0, 150) == 150  # capped
    assert steps_affordable(4.0, 2.0, 5.0, 150) == 0
    assert steps_affordable(10.0, 0.0, 5.0, 150) == 150


def test_checkpoint_choice_uses_validation_only() -> None:
    history = [
        {"step": 10, "path": "a", "val_brier": 0.20, "eval_brier": 0.10},
        {"step": 20, "path": "b", "val_brier": 0.18, "eval_brier": 0.30},
        {"step": 30, "path": "c", "val_brier": 0.18, "eval_brier": 0.01},
    ]
    best = pick_checkpoint(history)
    assert best is not None and best["path"] == "b"  # best validation, earliest on ties
    assert pick_checkpoint([]) is None


def test_early_stop_and_hacking_guard() -> None:
    h = [{"step": s, "val_brier": v} for s, v in [(10, 0.20), (20, 0.19), (30, 0.21), (40, 0.22)]]
    assert not should_stop(h[:3], patience=3)
    assert should_stop(h + [{"step": 50, "val_brier": 0.20}], patience=3)
    assert not should_stop(h + [{"step": 50, "val_brier": 0.18}], patience=3)
    first = {"dropped": 0.4, "tool_calls": 3.0}
    assert hacking_signal(first, {"dropped": 0.5, "tool_calls": 2.5}) is None
    assert "verifier" in (hacking_signal(first, {"dropped": 1.2, "tool_calls": 3.0}) or "")
    assert "tool calls" in (hacking_signal(first, {"dropped": 0.1, "tool_calls": 0.1}) or "")


def test_batches_mix_task_kinds() -> None:
    import random

    pool = [_task(i, "train", f"c{i}", kind="spike_hold") for i in range(30)] + [
        _task(100 + i, "train", f"r{i}", kind="resolve") for i in range(30)
    ]
    b = make_batch(pool, 15, 2 / 3, random.Random(0))
    assert len(b) == 15 and sum(t.kind == "spike_hold" for t in b) == 10


def test_trade_status_is_written_per_batch_and_flow_refuses_incomplete_walks(
    tmp_path: Path,
) -> None:
    from gridcase.backfill.jobs import run_trades

    t_need = T0 + 3 * 86400

    def handler(req: httpx.Request) -> httpx.Response:
        cid = req.url.params["condition"]
        cursor = req.url.params.get("cursor")
        page = int(cursor) if cursor else 0
        ts = T0 + 9 * 86400 - page * 86400  # one day per page, newest first
        rows = [{"proxy_wallet": "0xw", "side": "BUY", "token_id": "t0", "size": 10.0,
                 "price": 0.5, "timestamp": ts, "outcome": "Yes", "outcome_index": 0,
                 "transaction_hash": f"0x{cid}{page}", "title": "Q0?", "slug": "m0",
                 "event_slug": "e1"}]  # fmt: skip
        return httpx.Response(
            200, json={"data": rows, "pagination": {"next_cursor": str(page + 1)}}
        )

    src = tmp_path / "src"
    needed = {"0xm0": t_need, "0xm1": t_need, "0xm2": t_need}
    res = run_trades(_mock_client(tmp_path, handler), src, needed, max_pages=3, workers=1, batch=2,
                     progress=lambda _: None)  # fmt: skip
    assert res["incomplete"] == 3  # 3 pages reach day 7, not day 3
    assert len(list((src / "trades_status").glob("part-*.parquet"))) == 2  # one per batch
    write_parquet(_hist_markets(), MARKET_COLUMNS, src / "markets.parquet")
    write_parquet(_hist_prices(1), PRICE_COLUMNS, src / "prices" / "part-00000.parquet")
    build(src, tmp_path / "b.duckdb")
    con = gc_connect(tmp_path / "b.duckdb", read_only=True)
    when = datetime.fromtimestamp(T0 + 4 * 86400, UTC).replace(tzinfo=None)
    out = run_tool(con, "hist.flow", {"condition_id": "0xm0", "hours": 24}, when).output
    assert out["trade_data"] is False and "not backfilled" in out["reason"]
    con.close()


# ---------- step 1 after the post-mortem: fresh holdout, market-implied baseline, no close cue ----
#
# The 600 eval tasks were studied in the post-mortem, so final grading moves to a fresh holdout
# drawn from the same eval window that shares no news cluster and no market with them, frozen and
# protected like the first set. The spike_hold "market" row becomes the price's own implication
# (price_now for an up-move, 1 - price_now for a down-move); the old bucket rule stays as
# bucket_rate. The resolve prompt no longer says when the market closes: a forecaster knows the
# scheduled end (hist.market), not that a market will close early.


def _spike(i: int, cluster: str, cid: str, cat: str = "Politics", kind: str = "spike_hold") -> Task:
    return Task(
        task_id=f"{kind}:{cid}:{i}", kind=kind, condition_id=cid,  # type: ignore[arg-type]
        as_of=datetime(2026, 8, 1), facts={}, cluster_id=cluster, split="eval", category=cat,
    )  # fmt: skip


def test_market_baseline_is_what_the_price_implies() -> None:
    from gridcase.rl.harness import bucket_rate_baseline

    rates = {"up:20-30": 0.44, "down:20-30": 0.78, "spike_hold": 0.61, "resolve": 0.46}

    def task(kind: str, facts: dict[str, Any]) -> Task:
        return Task(task_id="t", kind=kind, condition_id="0x1", as_of=datetime(2026, 8, 1),  # type: ignore[arg-type]
                    facts=facts, cluster_id="c", split="eval", category="Politics")  # fmt: skip

    assert market_baseline(task("resolve", {"price_now": 0.3}), rates) == pytest.approx(0.3)
    up = task("spike_hold", {"price_now": 0.6, "move_pts": 20.0})
    down = task("spike_hold", {"price_now": 0.3, "move_pts": -20.0})
    assert market_baseline(up, rates) == pytest.approx(0.6)
    assert market_baseline(down, rates) == pytest.approx(0.7)
    assert bucket_rate_baseline(up, rates) == pytest.approx(0.44)  # the old rule, kept
    assert bucket_rate_baseline(down, rates) == pytest.approx(0.78)


def test_resolve_prompt_does_not_say_when_the_market_closes() -> None:
    p = prompt_for(_resolve_task(T0 + 6 * 86400, 0.42))
    assert "0.42" in p and "first outcome" in p
    assert "close" not in p.split("as_of")[1] and "hours" not in p


def test_holdout_shares_no_cluster_or_market_with_the_studied_set() -> None:
    from gridcase.rl.tasks import pick_holdout

    used = [_spike(0, "c0", "0xa"), _spike(1, "c1", "0xb", kind="resolve")]
    pool = [
        *used,
        _spike(2, "c0", "0xz"),  # same news cluster as a used task
        _spike(3, "c9", "0xb", kind="resolve"),  # same market as a used task
        *[_spike(i, f"n{i}", f"0x{i}", "Sports" if i % 2 else "Crypto") for i in range(10, 40)],
        *[_spike(i, f"n{i}", f"0x{i}", kind="resolve") for i in range(40, 60)],
    ]
    picked = pick_holdout(pool, used, n_spike=10, n_resolve=5, sports_cap=0.4, seed=16)
    assert sum(t.kind == "spike_hold" for t in picked) == 10
    assert sum(t.kind == "resolve" for t in picked) == 5
    used_clusters = {t.cluster_id for t in used}
    used_markets = {t.condition_id for t in used}
    assert not any(t.cluster_id in used_clusters or t.condition_id in used_markets for t in picked)
    assert len({t.cluster_id for t in picked}) == len(picked)  # one task per news cluster
    assert sum(t.category == "Sports" for t in picked if t.kind == "spike_hold") <= 4
    assert picked == pick_holdout(pool, used, 10, 5, 0.4, 16)  # deterministic


def test_a_frozen_holdout_cannot_be_silently_replaced(tmp_path: Path) -> None:
    from gridcase.rl.cli import FrozenMismatch, check_frozen

    tasks = [_spike(i, f"c{i}", f"0x{i}") for i in range(3)]
    answers = {t.task_id: Answer(task_id=t.task_id, y=float(i % 2), detail={})
               for i, t in enumerate(tasks)}  # fmt: skip
    hold, rl = tmp_path / "holdout", tmp_path / "rl"
    check_frozen(tasks, answers, hold)  # nothing committed yet: allowed
    sha = freeze(tasks, answers, hold, rl, answers_name="holdout_answers.jsonl")
    assert answers_sha(load_answers(rl / "holdout_answers.jsonl")) == sha
    assert not (rl / "answers.jsonl").exists()  # the first eval set's answers are untouched
    check_frozen(tasks, answers, hold)  # an identical rebuild passes
    flipped = {**answers, "spike_hold:0x0:0": Answer(task_id="spike_hold:0x0:0", y=1.0, detail={})}
    with pytest.raises(FrozenMismatch):
        check_frozen(tasks, flipped, hold)
    with pytest.raises(FrozenMismatch):
        check_frozen(tasks[:2], answers, hold)


# ---------- M15 step 2: a trainer that cannot collapse to a constant ----------
#
# The first run's tuned model answered 0.65 on 95% of spike tasks. Each change below is
# configurable in TrainConfig and is tested against its requirement: std-normalised group
# advantages (a 0.02 Brier gap still gives a unit-size signal; degenerate groups stay zero);
# per-episode length normalisation with an answer-turn weight (every episode carries the same
# total weight, prompt tokens stay zero); a KL penalty to the base model in the Cookbook's form
# plus a collapse guard; stratified batches that always carry both directions and the large-move
# buckets; a step count re-planned every step; one compact rollout row per training episode;
# and an offline toy that reproduces the collapse under the old scheme and learns under the new.


def test_std_normalised_advantages_keep_unit_scale_signal() -> None:
    from gridcase.rl.train import group_advantages

    old = group_advantages([1.0, 0.0, 0.5, 0.5], norm="mean")
    assert old == pytest.approx([0.5, -0.5, 0.0, 0.0])  # the first run's rule, kept as an option
    sharp = [0.8775, 0.8775, 0.8775, 0.8844, 0.8775, 0.8775, 0.8775, 0.8775]  # one 0.66, seven 0.65
    assert max(abs(a) for a in group_advantages(sharp, norm="mean")) < 0.01  # ~70x too small
    new = group_advantages(sharp, norm="std")
    assert max(new) > 1.5 and sum(new) == pytest.approx(0.0, abs=1e-9)
    assert new.index(max(new)) == 3
    assert group_advantages([0.7, 0.7, 0.7], norm="std") == [0.0, 0.0, 0.0]  # degenerate
    assert group_advantages([], norm="std") == []
    wide = group_advantages([1.0, 0.0], norm="std")
    assert wide == pytest.approx([1.0, -1.0], abs=0.01)  # (r - mean) / (std + eps)


def test_episode_weights_sum_to_one_and_favour_the_answer_turn() -> None:
    from gridcase.rl.train import episode_datums, turn_weights

    def turn(n_prompt: int, n: int) -> dict[str, Any]:
        return {"prompt_tokens": list(range(n_prompt)), "tokens": [7] * n, "logprobs": [-0.1] * n}

    short = [turn(10, 4), turn(20, 6)]  # tool turn, then the answer turn
    long = [turn(10, 40), turn(60, 40), turn(120, 40), turn(200, 20)]
    for turns in (short, long):
        w = turn_weights(turns, answer_turn=len(turns) - 1, answer_weight=4.0, length_norm=True)
        datums = episode_datums(turns, 1.0, w)
        total = sum(sum(abs(a) for a in d.advantages) for d in datums)
        assert total == pytest.approx(1.0)  # equal total weight whatever the token count
        for d, t in zip(datums, turns, strict=True):
            n_prompt = len(t["prompt_tokens"]) - 1
            assert all(a == 0.0 for a in d.advantages[:n_prompt])  # prompt stays zero
            assert all(wt == 0.0 for wt in d.weights[:n_prompt])
            assert len(set(d.advantages[n_prompt:])) == 1  # uniform within a turn
        per_tok_tool, per_tok_ans = datums[0].advantages[-1], datums[-1].advantages[-1]
        assert per_tok_ans == pytest.approx(4.0 * per_tok_tool)
    # a 2-token answer among 400 tokens still carries 4 / (4 + 1) ... of nothing: the weight is
    # per token, so the answer turn's share is 4 n_ans / (4 n_ans + n_other)
    w = turn_weights(long, answer_turn=3, answer_weight=4.0, length_norm=True)
    share = w[3] * 20 / (w[3] * 20 + sum(w[i] * 40 for i in range(3)))
    assert share == pytest.approx(80 / 200)
    # without normalisation (the first run) every sampled token carried the whole advantage
    plain = episode_datums(short, 0.5, None)
    assert plain[0].advantages[-1] == 0.5 and plain[1].advantages[-1] == 0.5
    # no answer turn (malformed episode): tool turns still count, nothing is up-weighted
    assert turn_weights(short, answer_turn=None, answer_weight=4.0, length_norm=True) == [
        pytest.approx(0.1),
        pytest.approx(0.1),
    ]
    # a turn without logprobs is skipped by episode_datums and gets no weight
    skipped = [turn(10, 4), {"prompt_tokens": [1, 2], "tokens": [3], "logprobs": []}]
    assert turn_weights(skipped, answer_turn=1, answer_weight=4.0, length_norm=True)[1] == 0.0
    assert len(episode_datums(skipped, 1.0, None)) == 1


def test_degenerate_groups_still_get_the_entropy_term() -> None:
    from gridcase.rl.agent import Episode
    from gridcase.rl.train import TrainConfig, apply_kl, step_datums

    def ep(n: int) -> Episode:
        e = Episode(task_id="t", model="m", probability=0.6, turns=1, answer_turn=0)
        e.raw_turns = [{"prompt_tokens": [1, 2, 3], "tokens": [4] * n, "logprobs": [-0.2] * n}]
        return e

    groups = [[ep(2), ep(3)], [ep(2), ep(2)]]
    advs = [[1.0, -1.0], [0.0, 0.0]]  # the second group is degenerate
    assert len(step_datums(groups, advs, TrainConfig(kl_coef=0.0))) == 2
    data = step_datums(groups, advs, TrainConfig(kl_coef=0.1))
    assert len(data) == 4
    assert data[2].advantages == [0.0] * 4 and data[2].weights[-1] > 0  # trainable
    # the entropy term reshapes the degenerate group's tokens too
    refs: list[list[float | None]] = [[0.0] * len(d.logprobs) for d in data]
    apply_kl(data, refs, coef=0.1)
    assert data[2].advantages[-1] == pytest.approx(0.0, abs=1e-12)  # same logprob: no change
    data[2].logprobs[-1] = -3.0  # one token far rarer than the rest
    apply_kl(data, refs, coef=0.1)
    assert data[2].advantages[-1] > 0.0


def test_kl_penalty_follows_the_cookbook_form_on_weighted_tokens() -> None:
    # tinker_cookbook/rl/metrics.py::incorporate_kl_penalty adds coef * mask * (avg_diff - diff)
    # to the advantages, diff = logp_sampled - logp_base over action tokens. Here mask is the
    # per-token weight, so the KL term has the same per-episode scale as the reward advantage.
    from gridcase.rl.train import DatumSpec, apply_kl

    a = DatumSpec([1, 2, 3, 4], [2, 3, 4, 5], [0.0, 0.0, -0.5, -0.5], [0.0, 0.0, 0.3, 0.3],
                  [0.0, 0.0, 0.1, 0.1])  # fmt: skip
    b = DatumSpec([1, 2, 3, 4], [2, 3, 4, 5], [0.0, 0.0, -0.5, -0.5], [0.0, 0.0, -0.3, -0.3],
                  [0.0, 0.0, 0.1, 0.1])  # fmt: skip
    # reference logprobs over the sampled tokens: a's tokens have risen far above the base
    ref = {0: [None, None, -2.5, -2.5], 1: [None, None, -0.5, -0.5]}
    stats = apply_kl([a, b], [ref[0], ref[1]], coef=0.5)
    assert a.advantages[:2] == [0.0, 0.0] and b.advantages[:2] == [0.0, 0.0]  # prompt stays zero
    # diff_a = 2.0, diff_b = 0.0, avg = 1.0: a is pushed down, b up, by coef * w * 1.0 = 0.05
    assert a.advantages[2:] == pytest.approx([0.25, 0.25])
    assert b.advantages[2:] == pytest.approx([-0.25, -0.25])
    assert stats["kl_policy_base"] == pytest.approx(1.0)
    # the term is zero-mean under the weights: it reshapes, it does not add reward
    total = sum(x for d in (a, b) for x in d.advantages)
    assert total == pytest.approx(0.0, abs=1e-9)
    # coef 0 leaves the advantages alone
    c = DatumSpec([1, 2], [2, 3], [0.0, -0.1], [0.0, 0.7], [0.0, 1.0])
    apply_kl([c], [[None, -3.0]], coef=0.0)
    assert c.advantages == [0.0, 0.7]


def test_collapse_guard_reads_degenerate_groups_and_spike_spread() -> None:
    from gridcase.rl.train import TrainConfig, collapse_signal, step_metrics

    def group(kind: str, ps: list[float], ys: float = 1.0) -> list[dict[str, Any]]:
        return [{"kind": kind, "p": p, "reward": 1 - (p - ys) ** 2} for p in ps]

    healthy = [group("spike_hold", [0.3, 0.6, 0.8, 0.65]), group("spike_hold", [0.65, 0.7]),
               group("resolve", [0.4, 0.4]), group("spike_hold", [0.2, 0.9])]  # fmt: skip
    m = step_metrics(healthy)
    assert m["degenerate_frac"] == pytest.approx(0.25)
    assert m["degenerate_frac_spike"] == pytest.approx(0.0)
    assert m["spike_p_distinct"] == 7 and m["spike_p_std"] > 0.15
    collapsed = [group("spike_hold", [0.65] * 4), group("spike_hold", [0.65] * 4),
                 group("resolve", [0.4, 0.5]), group("spike_hold", [0.65, 0.66])]  # fmt: skip
    c = step_metrics(collapsed)
    assert c["degenerate_frac_spike"] == pytest.approx(2 / 3) and c["spike_p_distinct"] == 2
    assert c["spike_p_std"] < 0.01
    cfg = TrainConfig(collapse_degenerate_frac=0.5, collapse_distinct_min=3, collapse_patience=3)
    assert collapse_signal([m, c, c], cfg) is None  # not yet persistent
    sig = collapse_signal([m, c, c, c], cfg)
    assert sig is not None and "degenerate" in sig
    # a deterministic policy whose answers differ across tasks is not collapse: every group
    # is degenerate, but the answers spread, and the entropy term still acts on those groups
    sharp = [group("spike_hold", [0.2] * 4), group("spike_hold", [0.9] * 4),
             group("spike_hold", [0.55] * 4), group("spike_hold", [0.7] * 4)]  # fmt: skip
    d = step_metrics(sharp)
    assert d["degenerate_frac_spike"] == 1.0 and d["spike_p_std"] > 0.2
    assert collapse_signal([d, d, d, d], cfg) is None
    assert collapse_signal([c, c, m], cfg) is None  # a healthy step resets the streak
    # too few distinct spike answers also counts, even with spread inside groups
    few = [
        group("spike_hold", [0.65, 0.7]),
        group("spike_hold", [0.7, 0.65]),
        group("resolve", [0.3, 0.6]),
    ]
    f = step_metrics(few)
    assert f["degenerate_frac_spike"] == 0.0 and f["spike_p_distinct"] == 2
    assert "distinct" in (collapse_signal([f, f, f], cfg) or "")
    # empty step (every episode errored)
    assert step_metrics([])["degenerate_frac"] == 0.0


def test_stratified_batches_cover_both_directions_and_large_moves() -> None:
    import random

    from gridcase.rl.harness import bucket
    from gridcase.rl.train import TrainConfig, make_batch

    def spike(i: int, move: float) -> Task:
        return Task(task_id=f"s{i}", kind="spike_hold", condition_id=f"0x{i}",  # type: ignore[arg-type]
                    as_of=datetime(2026, 8, 1), facts={"move_pts": move, "price_now": 0.5},
                    cluster_id=f"c{i}", split="train", category="Politics")  # fmt: skip

    # a pool like the train split: large down-moves are rare (~3%), large up-moves rarer
    moves = [12.0] * 300 + [-12.0] * 100 + [17.0] * 80 + [-17.0] * 60 + [24.0] * 60 + [-24.0] * 30
    moves += [35.0] * 10 + [-35.0] * 10
    pool = [spike(i, m) for i, m in enumerate(moves)] + [
        _task(1000 + i, "train", f"r{i}", kind="resolve") for i in range(300)
    ]
    rng = random.Random(0)
    misses = 0
    for _ in range(50):
        b = make_batch(pool, 16, 2 / 3, rng, stratify=True)
        assert len(b) == 16 and sum(t.kind == "resolve" for t in b) == 5  # resolve share unchanged
        spikes = [t for t in b if t.kind == "spike_hold"]
        assert len({t.task_id for t in b}) == 16
        buckets = {bucket(float(t.facts["move_pts"])) for t in spikes}
        assert {"up:30-101", "down:30-101"} <= buckets  # large moves in every step
        assert any(k.startswith("up") for k in buckets) and any(
            k.startswith("down") for k in buckets
        )
        misses += len(buckets) < 8
    assert misses == 0  # 11 spike slots cover all 8 direction x size buckets
    # uniform sampling (the first run) misses the large-move buckets most steps
    plain_misses = 0
    for _ in range(50):
        plain = make_batch(pool, 16, 2 / 3, rng, stratify=False)
        seen = {bucket(float(t.facts["move_pts"])) for t in plain if t.kind == "spike_hold"}
        plain_misses += "down:30-101" not in seen
    assert plain_misses > 25
    # fewer spike slots than buckets: still one per bucket as far as they go, no duplicates
    small = make_batch(pool, 6, 2 / 3, rng, stratify=True)
    assert len(small) == 6 and len({t.task_id for t in small}) == 6
    # group size is a config knob and sets the episodes per step
    assert TrainConfig(group_size=16).group_size == 16
    assert TrainConfig().episodes_per_step == 16 * 8
    assert TrainConfig(group_size=16, tasks_per_step=12).episodes_per_step == 192


def test_steps_are_replanned_every_step_from_the_running_average() -> None:
    from gridcase.rl.train import plan_steps

    # after 3 steps at $2.04 each with $85 left: 3 + floor((85 - 5) / 2.04) = 42
    assert plan_steps(step=3, spent=6.12, remaining=78.88, reserve=5.0, cap=150) == 3 + 36
    # steps got cheaper (~$1): the plan grows instead of leaving $43 unused
    assert plan_steps(step=20, spent=25.0, remaining=60.0, reserve=5.0, cap=150) == 20 + 44
    # steps got dearer: the plan shrinks, the reserve is kept
    assert plan_steps(step=10, spent=30.0, remaining=20.0, reserve=5.0, cap=150) == 10 + 5
    assert plan_steps(step=10, spent=30.0, remaining=5.0, reserve=5.0, cap=150) == 10
    assert plan_steps(step=10, spent=0.0, remaining=50.0, reserve=5.0, cap=150) == 150  # free: cap
    assert plan_steps(step=0, spent=0.0, remaining=50.0, reserve=5.0, cap=150) == 150
    assert plan_steps(step=100, spent=10.0, remaining=500.0, reserve=5.0, cap=120) == 120


def test_every_training_episode_leaves_one_compact_rollout_row(tmp_path: Path) -> None:
    from gridcase.rl.agent import Episode
    from gridcase.rl.train import append_rollouts, rollout_row

    task = Task(task_id="s1", kind="spike_hold", condition_id="0x1", as_of=datetime(2026, 8, 1),  # type: ignore[arg-type]
                facts={"move_pts": -32.0, "price_now": 0.2}, cluster_id="c", split="train",
                category="Politics")  # fmt: skip
    ep = Episode(task_id="s1", model="m", probability=0.8, tool_calls=3, turns=4,
                 transcript=[{"role": "user", "content": "x" * 10_000}])  # fmt: skip
    ep.raw_turns = [
        {"prompt_tokens": list(range(5000)), "tokens": [1] * 50, "logprobs": [-0.1] * 50}
    ]
    row = rollout_row(step=7, task=task, ep=ep, reward=0.96, advantage=1.2)
    assert row == {
        "step": 7, "task_id": "s1", "kind": "spike_hold", "bucket": "down:30-101", "p": 0.8,
        "reward": 0.96, "turns": 4, "tool_calls": 3, "malformed": False, "penalised": False,
        "advantage": 1.2, "tokens": 50, "error": None,
    }  # fmt: skip
    res = Task(task_id="r1", kind="resolve", condition_id="0x2", as_of=datetime(2026, 8, 1),  # type: ignore[arg-type]
               facts={"price_now": 0.4}, cluster_id="c", split="train",
               category="Politics")  # fmt: skip
    bad = Episode(task_id="r1", model="m", malformed=True, turns=2)
    row2 = rollout_row(step=7, task=res, ep=bad, reward=-0.25, advantage=-0.9)
    assert row2["bucket"] == "resolve" and row2["p"] is None and row2["malformed"]
    failed = Episode(task_id="r1", model="m", malformed=True, error="RuntimeError: 503")
    assert rollout_row(step=7, task=res, ep=failed, reward=-0.25, advantage=0.0)["error"] == (
        "RuntimeError: 503"
    )
    path = tmp_path / "rl" / "train_rollouts.jsonl"
    append_rollouts(path, [row, row2])
    append_rollouts(path, [row])
    lines = path.read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0]) == row and len(lines[0]) < 300  # compact
    assert json.loads(lines[1])["p"] is None


def test_per_group_std_normalisation_is_biased_for_a_brier_reward() -> None:
    # Samples in a group share the task's outcome. Around a sharp answer p, a y = 1 group's
    # rewards spread 2(1 - p) per unit of p and a y = 0 group's 2p: that ratio is what tells the
    # policy which way is cheaper. Per-group std normalisation makes both unit-size, so the
    # expected push is (2P - 1) x spread, toward an extreme, not toward P. Normalising by the
    # step's pooled spread keeps the ratio.
    from gridcase.rl.train import batch_advantages

    ps = [0.79, 0.80, 0.81]
    y1 = [1 - (p - 1) ** 2 for p in ps]
    y0 = [1 - (p - 0) ** 2 for p in ps]
    per_group = batch_advantages([y1, y0], norm="group_std", eps=0.0)
    assert per_group[0][2] == pytest.approx(-per_group[1][2], rel=0.02)  # same size, opposite
    pooled = batch_advantages([y1, y0], norm="batch_std", eps=0.0)
    assert pooled[0][2] / -pooled[1][2] == pytest.approx(0.2 / 0.8, rel=0.02)
    mean = batch_advantages([y1, y0], norm="mean")
    assert mean[0][2] / -mean[1][2] == pytest.approx(0.2 / 0.8, rel=0.02)
    rms = math.sqrt(sum(a * a for g in pooled for a in g) / 6)
    assert rms == pytest.approx(1.0)  # unit RMS over the step
    assert batch_advantages([[0.5, 0.5], y1], norm="batch_std")[0] == [0.0, 0.0]  # degenerate


def test_toy_old_scheme_sharpens_to_a_constant_and_new_scheme_keeps_learning() -> None:
    # experiments/rl_toy: a policy over a grid of answers with a shared bias and feature terms
    # (direction, headroom, size), groups of samples that share the task's outcome, Adam with
    # Tinker's defaults, degenerate groups dropped. Rewards from the committed base rates and
    # the post-mortem's headroom curve. The old scheme (reward minus group mean, token-sum,
    # uniform batches, no entropy term) sharpens onto two grid bins and most groups go
    # degenerate; the new one (batch-normalised, length-normalised, entropy bonus, stratified)
    # keeps the spread, keeps learning, and ends closer to the direction/headroom rule.
    from gridcase.rl.toy import NEW, OLD, simulate

    seeds = (1, 2, 3)
    old = [simulate(OLD, steps=120, seed=s) for s in seeds]
    new = [simulate(NEW, steps=120, seed=s) for s in seeds]
    const, rule = old[0].constant_brier, old[0].rule_brier
    assert const > rule + 0.03  # the toy world has something to learn
    assert new[0].rule_brier == pytest.approx(rule)
    mean = lambda rs, f: sum(f(r) for r in rs) / len(rs)  # noqa: E731
    # old: two live answers, half the groups degenerate, the direction gap (truth ~0.28) unlearned
    assert all(r.spike_p_distinct <= 3 for r in old)
    assert all(r.degenerate_frac_last >= 0.3 for r in old)
    assert all(r.mean_p["down"] - r.mean_p["up"] < 0.1 for r in old)
    # new: spread alive, fewer degenerate groups, better Brier, steeper headroom answers
    assert all(r.spike_p_distinct >= 5 for r in new)
    assert all(r.degenerate_frac_last < 0.3 for r in new)
    assert mean(new, lambda r: r.brier) < mean(old, lambda r: r.brier)
    assert mean(new, lambda r: r.brier) < const - 0.01
    assert all(r.headroom_corr < -0.7 for r in new)
    h_gap = lambda r: r.mean_p["headroom <=0.05"] - r.mean_p["headroom >0.5"]  # noqa: E731
    assert mean(new, h_gap) > mean(old, h_gap) + 0.05
    assert mean(new, lambda r: r.mean_p["down"]) > mean(old, lambda r: r.mean_p["down"])


def test_train_cli_flags_map_onto_the_config() -> None:
    from gridcase.cli import main as cli_main  # noqa: F401  (argparse wiring lives there)
    from gridcase.rl.train import TrainConfig, config_from_args

    class Args:
        tasks_per_step = 12
        group_size = 16
        max_steps = 12
        learning_rate = None
        kl_coef = 0.05
        kl_target = "uniform"
        stratify = None  # flag not given: config default
        length_norm = False

    cfg = config_from_args(Args())
    assert (cfg.tasks_per_step, cfg.group_size, cfg.max_steps) == (12, 16, 12)
    assert cfg.learning_rate == TrainConfig().learning_rate
    assert cfg.kl_coef == 0.05 and cfg.kl_target == "uniform"
    assert cfg.stratify is True and cfg.length_norm is False
    assert cfg.episodes_per_step == 192


def test_diagnose_reads_direction_and_headroom_from_a_results_file(tmp_path: Path) -> None:
    from gridcase.rl.diagnose import diagnose, headroom
    from gridcase.rl.tasks import write_manifest

    def spike(i: int, move: float, price: float) -> Task:
        return Task(task_id=f"s{i}", kind="spike_hold", condition_id=f"0x{i}",  # type: ignore[arg-type]
                    as_of=datetime(2026, 8, 1), facts={"move_pts": move, "price_now": price},
                    cluster_id=f"c{i}", split="validation", category="Politics")  # fmt: skip

    tasks = [spike(0, 20.0, 0.9), spike(1, 20.0, 0.6), spike(2, -20.0, 0.1), spike(3, -20.0, 0.4)]
    assert [headroom(t) for t in tasks] == pytest.approx([0.1, 0.4, 0.1, 0.4])
    tasks.append(_task(9, "validation", "r", kind="resolve"))
    write_manifest(tasks, tmp_path / "manifest.jsonl")
    # answers rise after down-moves and fall with headroom: a model that read the prompt
    rows = [
        {"task_id": "s0", "p": 0.55, "y": 1.0}, {"task_id": "s1", "p": 0.45, "y": 0.0},
        {"task_id": "s2", "p": 0.9, "y": 1.0}, {"task_id": "s3", "p": 0.7, "y": 1.0},
        {"task_id": "t9", "p": 0.3, "y": 0.0},
    ]  # fmt: skip
    (tmp_path / "res.json").write_text(json.dumps({"rows": rows}))
    d = diagnose(tmp_path / "res.json", tmp_path / "manifest.jsonl")
    assert d["n"] == 4 and d["distinct_p"] == 4
    assert d["mean_p_up"] == pytest.approx(0.5) and d["mean_p_down"] == pytest.approx(0.8)
    assert d["direction_corr"] > 0.8 and d["headroom_corr"] < -0.3
    assert d["brier"] < d["best_constant_brier"]
    # a constant answer: no correlation, one distinct value, Brier at the constant's level
    const = [{**r, "p": 0.65} for r in rows]
    (tmp_path / "c.json").write_text(json.dumps({"rows": const}))
    c = diagnose(tmp_path / "c.json", tmp_path / "manifest.jsonl")
    assert c["direction_corr"] == 0.0 and c["headroom_corr"] == 0.0 and c["distinct_p"] == 1


# ---------- M15 step 3: BD framing (D1), unchecked summary, facts receipt (D2) ----------
#
# User decisions 2026-10-14 (docs/M15_FINETUNING_GUIDE.md §6): a spike that fell is shown as a
# rise of the market's second outcome, in the prompt and in the own-market tools, and the prompt
# states the market-implied hold probability q. The submit summary is no longer a checked claim;
# the task's facts are a receipt claims may cite. The benchmark verifier matches numbers strictly
# (a probability no longer "verifies" against any price within 0.5); the product verifier is
# unchanged.


def _down_spike(db: Path, as_of_ts: int, drop: float = 0.2) -> Task:
    con = gc_connect(db, read_only=True)
    as_of = datetime.fromtimestamp(as_of_ts, UTC).replace(tzinfo=None)
    now = float(run_tool(con, "hist.move", {"condition_id": "0xm0"}, as_of).output["price"])
    con.close()
    before = round(now + drop, 4)
    return Task(
        task_id=f"spike_hold:0xm0:{as_of_ts}", kind="spike_hold", condition_id="0xm0",
        as_of=as_of, cluster_id="news:x", split="eval", category="Politics",
        facts={"move_pts": round((now - before) * 100, 1), "window_hours": 6,
               "price_before": before, "price_now": now, "horizon_hours": 72},
    )  # fmt: skip


def _results(ep: Any) -> dict[str, dict[str, Any]]:
    """Tool name -> output, from an episode's transcript."""
    out = {}
    for step in ep.transcript:
        if step["role"] == "tool":
            for (name, _), (_, content) in zip(step["calls"], step["results"], strict=True):
                out[name] = json.loads(content)
    return out


def test_bd_prompt_shows_a_drop_as_a_rise_of_the_other_outcome() -> None:
    t = Task(
        task_id="spike_hold:0x1:1", kind="spike_hold", condition_id="0x1",
        as_of=datetime(2026, 8, 1), cluster_id="c", split="eval", category="Politics",
        facts={"move_pts": -22.0, "window_hours": 6, "price_before": 0.49, "price_now": 0.27,
               "horizon_hours": 72},
    )  # fmt: skip
    bd = prompt_for(t, framing="bd", facts_id="r123")
    assert "second outcome" in bd and "from 0.51 to 0.73 (+22.0 points)" in bd
    assert "the price ends on the side this move points to is 0.730" in bd
    assert "receipt r123" in bd
    a = prompt_for(t, framing="a", facts_id="r123")
    assert "from 0.49 to 0.27 (-22.0 points)" in a and "For reference" not in a
    up = Task(**{**t.model_dump(), "facts": {**t.facts, "move_pts": 22.0, "price_before": 0.27,
                                               "price_now": 0.49}})  # fmt: skip
    bd_up = prompt_for(up, framing="bd")
    assert "the market's price moved from 0.27 to 0.49 (+22.0 points)" in bd_up
    assert "points to is 0.490" in bd_up


def test_bd_tools_report_the_own_market_in_the_shown_outcome(tmp_path: Path) -> None:
    db = _hist_db(tmp_path, seed=21)
    con = gc_connect(db, read_only=True)
    task = _down_spike(db, T0 + 6 * 86400)
    calls = [("hist_move", {"condition_id": "0xm0"}), ("hist_market", {"condition_id": "0xm0"}),
             ("hist_series", {"condition_id": "0xm0", "hours": 24, "grain": "1h"}),
             ("hist_siblings", {"condition_id": "0xm0", "hours": 6})]  # fmt: skip

    def policy(h: list[dict[str, Any]]) -> Turn:
        if len(h) == 1:
            return _turn(calls)
        move = json.loads(h[1]["results"][0][1])
        return _turn([("submit_answer", {
            "probability": 0.7, "summary": f"It rose to {move['output']['price']}.",
            "claims": [{"text": f"The price is {move['output']['price']}.",
                        "receipt_ids": [move["receipt_id"]]}],
        })])  # fmt: skip

    raw = {n: run_tool(con, n.replace("_", "."), a, task.as_of).output for n, a in calls}
    ep = run_episode(ScriptedAgent(policy), con, task, framing="bd")
    got = {k: v["output"] for k, v in _results(ep).items()}
    move = got["hist_move"]
    assert move["price"] == pytest.approx(1 - raw["hist_move"]["price"])
    assert move["price"] == pytest.approx(1 - task.facts["price_now"])  # matches the prompt
    assert f"to {round(1 - task.facts['price_now'], 4)}" in ep.transcript[0]["content"]
    for k, v in raw["hist_move"]["change"].items():
        assert move["change"][k] is None if v is None else move["change"][k] == pytest.approx(-v)
    assert move["prices_are_for"] == "No" and got["hist_market"]["price_is_for"] == "No"
    for (_, p0), (_, p1) in zip(raw["hist_series"]["points"], got["hist_series"]["points"],
                                strict=True):  # fmt: skip
        assert p1 == pytest.approx(1 - p0)
    assert got["hist_siblings"]["prices_are_for"] == "each market's first outcome"
    assert not ep.dropped and not ep.penalised and ep.probability == 0.7
    plain = run_episode(ScriptedAgent(policy), con, task, framing="a")
    assert _results(plain)["hist_move"]["output"] == raw["hist_move"]
    con.close()


def test_summary_is_not_checked_and_task_facts_are_citable(tmp_path: Path) -> None:
    db = _hist_db(tmp_path, seed=22)
    con = gc_connect(db, read_only=True)
    task = _resolve_task(T0 + 6 * 86400, 0.42)

    def policy(h: list[dict[str, Any]]) -> Turn:
        rid = h[0]["content"].split("receipt ")[1].split(".")[0]
        return _turn([("submit_answer", {
            "probability": 0.42, "summary": "At 0.42 the market gives it 42%.",
            "claims": [{"text": "The price now is 0.42.", "receipt_ids": [rid]}],
        })])  # fmt: skip

    ep = run_episode(ScriptedAgent(policy), con, task, framing="bd")
    assert ep.dropped == [] and not ep.penalised
    assert [c.text for c in ep.claims] == ["The price now is 0.42."]
    assert "not checked" in tool_specs()[-1]["description"]
    con.close()


def test_benchmark_matcher_is_strict_and_the_product_verifier_unchanged() -> None:
    from gridcase.explain.schema import Claim, Conclusion
    from gridcase.explain.verifier import verify
    from gridcase.ledger.schema import Receipt

    r = Receipt.build(tool="hist.move", inputs={}, as_of=datetime(2026, 8, 1),
                      output={"price": 0.4, "sd": 0.2735}, data_version=None, effect_size=None,
                      runtime_ms=0, sql=[])  # fmt: skip
    claims = [Claim(text="The answer is 0.65.", receipt_ids=[r.receipt_id]),
              Claim(text="The spread is about 0.27.", receipt_ids=[r.receipt_id])]  # fmt: skip
    concl = Conclusion(case_id="c", top_hypothesis=None, ranked=[], claims=claims)
    strict, dropped = verify(concl, {r.receipt_id: r}, drop_uncited_numbers=True, strict=True)
    assert [c.text for c in strict.claims] == ["The spread is about 0.27."] and len(dropped) == 1
    lenient, _ = verify(concl, {r.receipt_id: r}, drop_uncited_numbers=True)
    assert len(lenient.claims) == 2  # the product rule: whole-unit rounding on small numbers


def test_a_submit_written_as_text_is_still_read(tmp_path: Path) -> None:
    db = _hist_db(tmp_path, seed=23)
    con = gc_connect(db, read_only=True)
    task = _resolve_task(T0 + 6 * 86400, 0.42)
    for text in (
        'Done. {"name": "submit_answer", "arguments": {"probability": 0.3, "summary": "x", '
        '"claims": []}}',
        '{"probability": 0.3, "summary": "x", "claims": []}',
    ):
        t = Turn(text=text, calls=[])
        ep = run_episode(ScriptedAgent(lambda h, t=t: t), con, task)
        assert ep.probability == 0.3 and not ep.malformed and ep.parsed_from_text
    silent = run_episode(ScriptedAgent(lambda h: Turn(text="I think 0.3", calls=[])), con, task)
    assert silent.malformed  # no JSON: nudged once, then malformed, as before
    con.close()


def test_benchmark_matcher_excuses_windows_and_market_name_numbers() -> None:
    """User decision 2026-10-16: "over 24 hours" states a window, not a measured fact, and
    "the $78k strike" quotes a market name the evidence shows; neither is an unsupported number.
    A wrong measured number is still dropped; the product verifier is unchanged."""
    from gridcase.explain.schema import Claim, Conclusion
    from gridcase.explain.verifier import verify
    from gridcase.ledger.schema import Receipt

    sib = Receipt.build(
        tool="hist.siblings", inputs={}, as_of=datetime(2026, 8, 1),
        output={"siblings": [{"question": "Will Bitcoin be above $78,000 on Sept 11?",
                              "price": 0.905, "change": -0.02},
                             {"question": "Tweets 100-119?", "price": 0.1005, "change": 0.0}]},
        data_version=None, effect_size=None, runtime_ms=0, sql=[],
    )  # fmt: skip
    move = Receipt.build(
        tool="hist.move", inputs={}, as_of=datetime(2026, 8, 1),
        output={"condition_id": "0x04c227bcce5c98b2", "price": 0.57, "change": {"24h": 0.355}},
        data_version=None, effect_size=None, runtime_ms=0, sql=[],
    )  # fmt: skip
    receipts = {sib.receipt_id: sib, move.receipt_id: move}
    texts = [
        ("The market price is 0.57 and rose 0.355 over 24 hours.", [move.receipt_id], True),
        ("The $78k strike is at 0.905.", [sib.receipt_id], True),
        ("The 100-119 bucket was priced at 0.1005.", [sib.receipt_id], True),
        ("The price rose 0.42 over 24 hours.", [move.receipt_id], False),  # 0.42 is not in it
        ("There were 227 trades.", [move.receipt_id], False),  # digits of an id are not a name
    ]
    concl = Conclusion(case_id="c", top_hypothesis=None, ranked=[],
                       claims=[Claim(text=t, receipt_ids=r) for t, r, _ in texts])  # fmt: skip
    kept, _ = verify(concl, receipts, drop_uncited_numbers=True, strict=True)
    assert [c.text for c in kept.claims] == [t for t, _, ok in texts if ok]
    product, _ = verify(concl, receipts, drop_uncited_numbers=True)
    assert "The market price is 0.57 and rose 0.355 over 24 hours." not in [
        c.text for c in product.claims
    ]  # unchanged: the product rule still asks for the window to be in the evidence
