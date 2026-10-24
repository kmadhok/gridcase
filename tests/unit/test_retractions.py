"""Retracted cases: kept as judged, hidden from the public lists, still served on their page,
and still counted by the hourly watch's dedup and daily paid cap."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from gridcase import config
from gridcase.cli import main
from gridcase.investigate import investigate
from gridcase.ledger.schema import Receipt
from gridcase.ledger.store import write_receipt
from gridcase.market_alarm import WatchedSeries
from gridcase.market_watch import watch_markets
from gridcase.memory.cases import CaseRecord, get_case, list_cases, save_case
from gridcase.memory.retractions import (
    get_retraction,
    outage_cases,
    retract_case,
    retractions,
    unretract_case,
)
from gridcase.planner.llm import FakeLLM, LLMUsage
from gridcase.server.app import create_app
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF, START, add_trades, one_market_tripled

SPORTS = WatchedSeries(kpi="volume_usd", filters={"category": "Sports"})


def _plant(
    conn: duckdb.DuckDBPyConnection,
    case_id: str,
    window: tuple[datetime, datetime],
    as_of: datetime | None = None,
    cost: float = 0.0,
) -> None:
    """A case with only a kpi.compare receipt for `window`, judged at `as_of` (window end + 30m)."""
    at = as_of or window[1] + timedelta(minutes=30)
    save_case(
        conn,
        CaseRecord(
            case_id=case_id,
            entity="all",
            metric="volume_usd",
            opened_at=at,
            as_of=at,
            state="action_taken",
            season="autumn",
            llm_usage=LLMUsage(cost_usd=cost),
        ),
    )
    r = Receipt.build(
        "kpi.compare",
        {"kpi": "volume_usd", "window": [window[0].isoformat(), window[1].isoformat()]},
        at,
        {"value": 0.0},
        None,
        None,
        0,
        [],
    )
    write_receipt(conn, case_id, r)


def _sports_case(store: Path) -> str:
    conn = connect(store)
    add_trades(conn, one_market_tripled())
    case = investigate(conn, FakeLLM(), "volume_usd", AS_OF, filters={"category": "Sports"})
    conn.close()
    return case.case_id


def test_retract_and_unretract_round_trip(market_store: Path) -> None:
    conn = connect(market_store)
    _plant(conn, "c1", (START + timedelta(days=2), START + timedelta(days=2, hours=2)))
    before = get_case(conn, "c1")
    gap = (START + timedelta(days=2), START + timedelta(days=2, hours=1))
    r = retract_case(conn, "c1", "  data outage  ", gap, at=START + timedelta(days=5))
    assert (r.reason, r.span_from, r.span_to) == ("data outage", *gap)
    assert get_retraction(conn, "c1") == r
    # the case itself is untouched, and saving it again keeps the retraction
    assert get_case(conn, "c1") == before
    save_case(conn, before)
    assert get_retraction(conn, "c1") == r
    assert [c.case_id for c in list_cases(conn)] == ["c1"]  # list_cases still has it
    assert unretract_case(conn, "c1") is True
    assert get_retraction(conn, "c1") is None and retractions(conn) == {}
    assert unretract_case(conn, "c1") is False
    with pytest.raises(KeyError):
        retract_case(conn, "nope", "x")
    with pytest.raises(ValueError):
        retract_case(conn, "c1", "   ")
    conn.close()


def test_retracted_cases_leave_the_public_lists_but_their_page_says_why(
    market_store: Path,
) -> None:
    cid = _sports_case(market_store)
    conn = connect(market_store)
    _plant(conn, "other", (AS_OF - timedelta(hours=30), AS_OF - timedelta(hours=28)))
    retract_case(conn, cid, "data outage, later filled")
    conn.close()
    app = create_app(
        db_path=market_store,
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        clock=lambda: AS_OF,
    )
    with TestClient(app) as client:
        assert [c["case_id"] for c in client.get("/api/cases").json()] == ["other"]
        every = client.get("/api/cases", params={"include_retracted": 1}).json()
        assert [c["case_id"] for c in every] == [cid, "other"]
        assert every[0]["retracted"]["reason"] == "data outage, later filled"
        assert every[1]["retracted"] is None
        assert [c["case_id"] for c in client.get("/api/now").json()["cases"]] == ["other"]
        page = client.get(f"/api/cases/{cid}").json()
        assert page["case_id"] == cid and page["conclusion"] is not None
        assert page["retracted"]["reason"] == "data outage, later filled"
        assert page["retracted"]["at"] and page["retracted"]["span"] is None
        assert client.get("/api/cases/other").json()["retracted"] is None

    from mcp import Client

    async def drive() -> tuple[dict[str, Any], dict[str, Any]]:
        async with Client(app.state.mcp) as mcp:
            listed = await mcp.call_tool("list_open_cases", {})
            one = await mcp.call_tool("get_case", {"case_id": cid})
            return dict(listed.structured_content or {}), dict(one.structured_content or {})

    listed, one = asyncio.run(drive())
    assert [c["case_id"] for c in listed["cases"]] == ["other"]
    assert one["retracted"]["reason"] == "data outage, later filled"


def test_a_retracted_case_still_blocks_a_repeat_and_counts_toward_the_paid_cap(
    market_store: Path,
) -> None:
    conn = connect(market_store)
    add_trades(conn, one_market_tripled())
    first = watch_markets(conn, AS_OF, FakeLLM, FakeLLM, max_paid_per_day=10, watched=[SPORTS])
    assert len(first) == 1
    retract_case(conn, first[0], "data outage")
    # dedup: the same alarm minutes later is not investigated again
    again = AS_OF + timedelta(minutes=10)
    assert watch_markets(conn, again, FakeLLM, FakeLLM, max_paid_per_day=10, watched=[SPORTS]) == []

    # cap: a retracted paid case opened today still uses up today's one paid investigation
    conn.execute("DELETE FROM cases WHERE case_id = ?", [first[0]])
    _plant(conn, "paid", (AS_OF - timedelta(hours=40), AS_OF - timedelta(hours=39)), AS_OF, 0.5)
    retract_case(conn, "paid", "data outage")
    used: list[str] = []

    def paid() -> FakeLLM:
        used.append("paid")
        return FakeLLM()

    def free() -> FakeLLM:
        used.append("free")
        return FakeLLM()

    opened = watch_markets(conn, again, paid, free, max_paid_per_day=1, watched=[SPORTS])
    assert len(opened) == 1 and used == ["free"]
    conn.close()


def _outage_store(store: Path) -> tuple[duckdb.DuckDBPyConnection, datetime, datetime]:
    """Four planted cases: one overlapping a recorded gap, one whose hour was empty when it
    was judged and has been filled since, one on ordinary hours, one outside the range."""
    conn = connect(store)
    d2, d3, d4 = (START + timedelta(days=n, hours=10) for n in (2, 3, 4))
    conn.execute(
        "INSERT INTO collector_gaps VALUES (?, ?, NULL)",
        [d2 + timedelta(hours=1), d2 + timedelta(hours=1, minutes=20)],
    )
    _plant(conn, "gap", (d2, d2 + timedelta(hours=3)))
    _plant(conn, "filled", (d3, d3 + timedelta(hours=2)))
    conn.execute(  # hour d3+1h reached the store only after the case was judged
        "UPDATE trades SET collected_at = ? WHERE ts >= ? AND ts < ?",
        [d3 + timedelta(hours=5), d3 + timedelta(hours=1), d3 + timedelta(hours=2)],
    )
    _plant(conn, "fine", (d4, d4 + timedelta(hours=2)))
    _plant(conn, "early", (START, START + timedelta(hours=2)))
    conn.execute(
        "INSERT INTO collector_gaps VALUES (?, ?, NULL)",
        [START, START + timedelta(minutes=30)],
    )
    return conn, START + timedelta(days=1), START + timedelta(days=5)


def test_outage_matching_picks_exactly_the_cases_judged_on_missing_hours(
    market_store: Path,
) -> None:
    conn, since, until = _outage_store(market_store)
    found = {m.case_id: m for m in outage_cases(conn, since, until)}
    assert set(found) == {"gap", "filled"}
    d2, d3 = START + timedelta(days=2, hours=10), START + timedelta(days=3, hours=10)
    assert found["gap"].why == "collector gap"
    assert found["gap"].span == (d2 + timedelta(hours=1), d2 + timedelta(hours=1, minutes=20))
    assert found["filled"].why == "empty then, filled since"
    assert found["filled"].span == (d3 + timedelta(hours=1), d3 + timedelta(hours=2))
    assert not any(m.retracted for m in found.values())
    conn.close()


def test_retract_outage_dry_run_writes_nothing_and_the_real_run_retracts(
    market_store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn, since, until = _outage_store(market_store)
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", market_store)
    rng = ["--since", since.isoformat() + "Z", "--until", until.isoformat() + "Z"]
    before = market_store.read_bytes()
    assert main(["cases", "retract-outage", *rng, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "gap " in out and "filled " in out and "fine" not in out and "early" not in out
    assert "dry run, nothing written" in out
    assert market_store.read_bytes() == before  # opened read-only

    assert main(["cases", "retract-outage", *rng]) == 0
    assert "2 cases retracted" in capsys.readouterr().out
    conn = connect(market_store)
    done = retractions(conn)
    assert set(done) == {"gap", "filled"}
    assert done["filled"].reason.startswith("Data outage")
    assert done["filled"].span_from == START + timedelta(days=3, hours=11)
    conn.close()
    assert main(["cases", "retract-outage", *rng]) == 0  # running it again changes nothing
    assert "0 cases retracted (2 already were)" in capsys.readouterr().out

    assert main(["cases", "unretract", "gap", "fine"]) == 0
    out = capsys.readouterr().out
    assert "unretracted gap" in out and "fine was not retracted" in out
    assert main(["cases", "retract", "fine", "early", "--reason", "noise"]) == 0
    assert "retracted early: noise" in capsys.readouterr().out
    assert main(["cases", "retract", "nope", "--reason", "noise"]) == 1
    conn = connect(market_store)
    assert set(retractions(conn)) == {"filled", "fine", "early"}
    conn.close()


def test_a_store_made_before_retractions_gains_the_table_and_keeps_its_cases(
    market_store: Path,
) -> None:
    conn = connect(market_store)
    _plant(conn, "old", (START, START + timedelta(hours=2)))
    conn.execute("DROP TABLE case_retractions")  # as on a store from before this change
    conn.close()
    raw = duckdb.connect(str(market_store))
    assert raw.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'case_retractions'"
    ).fetchone() == (0,)
    raw.close()
    conn = connect(market_store)
    assert get_case(conn, "old").case_id == "old" and retractions(conn) == {}
    retract_case(conn, "old", "data outage")
    conn.close()
    conn = connect(market_store)  # and connecting again keeps it
    assert get_retraction(conn, "old") is not None
    conn.close()
