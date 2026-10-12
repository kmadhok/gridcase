"""M15 exit: backfill, point-in-time history tools, a frozen eval set, and RL training.

Plan: docs/plans/2026-10-12_rl_backfill_eval_train.md. Each phase's contract is a section
below, written before its code.

Phase A, the spend ledger. Every paid model call (frontier evals, small-model evals, training)
reserves its cost first. The ledger is append-only JSONL, so it survives restarts; it refuses any
reservation that would pass the phase's allowance or the global cap ($150)."""

from __future__ import annotations

import json
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
