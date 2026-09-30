"""M6 exit: a morning read that answers the standing questions once a day.

On a synthetic 14-day record (2026-10-01..14, three markets trading every hour) with one planted
story per question, the digest for 2026-10-15 compares the last 7 days with the 7 before and
finds each story: Sports gaining share, a market trading 5 times its usual volume, the Fed
outcome swinging 30c -> 80c, 4 of 10 new traders coming back, a Sunday 19:00 peak, half the
money in 2 markets. Every sentence cites receipts and survives the verifier; no model is called;
the same day builds the same digest; nothing collected after as_of is used."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from gridcase.digest import build_digest, get_digest
from gridcase.explain.schema import Claim, Conclusion
from gridcase.explain.verifier import numbers_in, verify
from gridcase.ledger.schema import Receipt
from gridcase.planner.llm import FakeLLM
from gridcase.semantic.kpis import KpiQuery, run_kpi
from gridcase.server.app import create_app
from gridcase.store.db import connect
from tests.polymarket_synth import add_market, add_trades

START = datetime(2026, 10, 1)
DAY = date(2026, 10, 15)
MIDNIGHT = datetime(2026, 10, 15)
AS_OF = MIDNIGHT + timedelta(minutes=30)
CUR = (datetime(2026, 10, 8), MIDNIGHT)
YESTERDAY = datetime(2026, 10, 14)


def _record(conn: duckdb.DuckDBPyConnection) -> None:
    add_market(conn, "c1", "Sports", "England vs Czechia")
    add_market(conn, "c2", "Crypto", "Bitcoin Up or Down")
    add_market(conn, "c3", "Politics", "Election winner")
    add_market(conn, "c4", "Sports", "Chiefs vs Bills")
    add_market(conn, "c5", "Politics", "Fed decision", "Will the Fed cut?")
    add_market(conn, "c6", "Politics", "Senate race", "Will the Senate flip?")
    rows: list[dict[str, Any]] = []
    for h in range(14 * 24):
        t = START + timedelta(hours=h)
        for i, cid in enumerate(("c1", "c2", "c3")):
            for k in range(2):
                rows.append(
                    {
                        "ts": t + timedelta(minutes=10 * k + i),
                        "wallet": f"w{(h + i + k) % 30}",
                        "usd": 100.0,
                        "condition_id": cid,
                        "title": "Bitcoin Up or Down - hourly"
                        if cid == "c2"
                        else "Will it happen?",
                    }
                )
        if t >= CUR[0]:  # Sports gains share this week: England trades an extra $100 an hour
            rows.append({"ts": t + timedelta(minutes=30), "wallet": "w1", "usd": 100.0})
    # Chiefs vs Bills: $200 a day at noon on Oct 7..13, then $1,000 yesterday (5x usual)
    for d in range(6, 13):
        rows.append(
            {
                "ts": START + timedelta(days=d, hours=12, minutes=40),
                "wallet": "w2",
                "usd": 200.0,
                "condition_id": "c4",
            }
        )
    rows.append(
        {
            "ts": YESTERDAY + timedelta(hours=12, minutes=40),
            "wallet": "w2",
            "usd": 1000.0,
            "condition_id": "c4",
        }
    )
    # the Fed outcome swings 30c -> 80c yesterday over 25 trades (its mirror 70c -> 20c);
    # the Senate outcome swings too but on only 10 trades; a recurring round swings, excluded
    for j in range(25):
        ts = YESTERDAY + timedelta(hours=1, minutes=2 * j)
        p = 0.30 + 0.5 * j / 24
        rows.append(
            {
                "ts": ts,
                "wallet": f"w{j}",
                "usd": 5.0,
                "price": p,
                "condition_id": "c5",
                "asset": "c5-yes",
                "title": "Will the Fed cut?",
                "outcome": "Yes",
            }
        )
        rows.append(
            {
                "ts": ts + timedelta(seconds=30),
                "wallet": f"w{j}",
                "usd": 5.0,
                "price": 1 - p,
                "condition_id": "c5",
                "asset": "c5-no",
                "title": "Will the Fed cut?",
                "outcome": "No",
            }
        )
        rows.append(
            {
                "ts": ts + timedelta(seconds=40),
                "wallet": f"w{j}",
                "usd": 5.0,
                "price": 0.1 + 0.8 * j / 24,
                "condition_id": "c2",
                "asset": "c2-up",
                "title": "Bitcoin Up or Down - hourly",
                "outcome": "Up",
            }
        )
    for j in range(10):
        rows.append(
            {
                "ts": YESTERDAY + timedelta(hours=3, minutes=j),
                "wallet": "w3",
                "usd": 5.0,
                "price": 0.1 + 0.08 * j,
                "condition_id": "c6",
                "asset": "c6-yes",
                "title": "Will the Senate flip?",
            }
        )
    # ten wallets first trade on Oct 13; four of them come back on Oct 14
    for n in range(10):
        rows.append({"ts": datetime(2026, 10, 13, 9, n), "wallet": f"n{n}", "usd": 10.0})
    for n in range(4):
        rows.append(
            {"ts": YESTERDAY + timedelta(hours=9, minutes=n), "wallet": f"n{n}", "usd": 10.0}
        )
    # the busiest hour: Sunday Oct 11, 19:00; and one whale trade on Monday Oct 12
    for j in range(50):  # many ordinary trades, not a whale
        rows.append(
            {
                "ts": datetime(2026, 10, 11, 19, 5, j),
                "wallet": "w4",
                "usd": 100.0,
                "condition_id": "c3",
            }
        )
    rows.append(
        {"ts": datetime(2026, 10, 12, 15, 45), "wallet": "w5", "usd": 2000.0, "condition_id": "c3"}
    )
    # collected after as_of: must never count
    rows.append(
        {
            "ts": YESTERDAY + timedelta(hours=23, minutes=59),
            "wallet": "late",
            "usd": 1e6,
            "condition_id": "c3",
            "collected_at": AS_OF + timedelta(minutes=1),
        }
    )
    add_trades(conn, rows)


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("m6") / "digest.duckdb"
    conn = connect(path)
    _record(conn)
    build_digest(conn, DAY, AS_OF)
    conn.close()
    return path


@pytest.fixture()
def db(store: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(store)
    yield conn
    conn.close()


@pytest.fixture()
def digest(db: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    d = get_digest(db, DAY)
    assert d is not None
    return d


def card(d: dict[str, Any], cid: str) -> dict[str, Any]:
    return next(c for c in d["cards"] if c["id"] == cid)


def kpi(db: duckdb.DuckDBPyConnection, k: str, window: tuple[datetime, datetime], **kw: Any) -> Any:
    return run_kpi(db, KpiQuery(kpi=k, window=window, as_of=AS_OF, **kw))


def test_1_compares_the_last_seven_days_with_the_seven_before(digest: dict[str, Any]) -> None:
    assert digest["day"] == "2026-10-15" and digest["period_days"] == 7
    assert digest["windows"]["current"] == ["2026-10-08T00:00:00", "2026-10-15T00:00:00"]
    assert digest["windows"]["previous"] == ["2026-10-01T00:00:00", "2026-10-08T00:00:00"]
    assert digest["windows"]["yesterday"] == ["2026-10-14T00:00:00", "2026-10-15T00:00:00"]
    assert [c["id"] for c in digest["cards"]] == [
        "share_shift",
        "movers",
        "odds_swings",
        "whales",
        "retention",
        "rhythm",
        "concentration",
        "kind",
        "alarms",
    ]


def test_2_totals_are_this_period_against_the_last(
    digest: dict[str, Any], db: duckdb.DuckDBPyConnection
) -> None:
    items = {t["kpi"]: t for t in digest["totals"]["items"]}
    assert set(items) == {"volume_usd", "active_traders", "markets_traded", "new_traders"}
    (now,) = kpi(db, "volume_usd", CUR, grain="total")[0]
    assert items["volume_usd"]["now"] == pytest.approx(now)
    assert items["volume_usd"]["now"] < 1e6  # the trade collected after as_of is invisible
    assert items["new_traders"]["now"] == 10


def test_3_sports_gained_share_and_shares_add_up(digest: dict[str, Any]) -> None:
    c = card(digest, "share_shift")
    segs = {s["segment"]: s for s in c["data"]["segments"]}
    assert c["data"]["top_gainer"] == "Sports" and c["data"]["top_loser"] == "Crypto"
    assert sum(s["now"] for s in segs.values()) == pytest.approx(1.0, abs=1e-5)
    assert sum(s["before"] for s in segs.values()) == pytest.approx(1.0, abs=1e-5)
    assert segs["Sports"]["change"] > 0.05
    assert c["sentence"]["text"].startswith("Sports rose from")


def test_4_a_market_at_five_times_its_usual_leads_the_movers(digest: dict[str, Any]) -> None:
    top = card(digest, "movers")["data"]["markets"][0]
    assert top["market"] == "Chiefs vs Bills"
    assert top["now"] == pytest.approx(1000) and top["usual"] == pytest.approx(200)
    assert top["ratio"] == pytest.approx(5.0)


def test_5_the_biggest_odds_swing_once_per_market(digest: dict[str, Any]) -> None:
    swings = card(digest, "odds_swings")["data"]["swings"]
    top = swings[0]
    assert top["market"] == "Will the Fed cut?"
    assert (top["outcome"], top["open"], top["close"]) == ("Yes", 0.3, 0.8)  # the side that rose
    assert abs(top["close"] - top["open"]) == pytest.approx(0.5)
    markets = [s["market"] for s in swings]
    assert markets.count("Will the Fed cut?") == 1
    assert "Will the Senate flip?" not in markets  # 10 trades: too few
    assert not any("Up or Down" in m for m in markets)  # recurring rounds swing by design


def test_6_four_of_ten_new_traders_came_back(digest: dict[str, Any]) -> None:
    days = {d["day"]: d for d in card(digest, "retention")["data"]["days"]}
    assert days["2026-10-13"] == {"day": "2026-10-13", "new": 10, "came_back": 4, "rate": 0.4}
    assert "2026-10-14" not in days  # yesterday's newcomers can only be judged tomorrow


def test_7_the_busiest_hour_and_concentration(digest: dict[str, Any]) -> None:
    rhythm = card(digest, "rhythm")["data"]
    assert rhythm["peak"] == {"weekday": "Sunday", "hour": 19}
    assert len(rhythm["grid"]) == 7 and all(len(r["hours"]) == 24 for r in rhythm["grid"])
    conc = card(digest, "concentration")["data"]
    assert conc["markets_for_half"] == 2 and conc["markets"] == 6


def test_8_whale_and_recurring_shares_by_day_equal_the_kpi(
    digest: dict[str, Any], db: duckdb.DuckDBPyConnection
) -> None:
    for cid, k in (("whales", "whale_share"), ("kind", "recurring_share")):
        got = {d["day"]: d["value"] for d in card(digest, cid)["data"]["days"]}
        want = {t.date().isoformat(): v for t, v in kpi(db, k, CUR, grain="day")}
        assert got == pytest.approx(want, abs=1e-6)  # receipts keep 6 decimals
    assert card(digest, "whales")["data"]["peak_day"] == "2026-10-12"


def test_9_every_sentence_is_evidenced_and_verified(digest: dict[str, Any]) -> None:
    receipts = {r["receipt_id"]: Receipt.model_validate(r) for r in digest["receipts"]}
    sentences = [digest["headline"], *(c["sentence"] for c in digest["cards"])]
    claims = [Claim.model_validate(s) for s in sentences]
    for c in claims:
        if numbers_in(c.text):
            assert c.receipt_ids, c.text
    _, dropped = verify(
        Conclusion(case_id="digest", top_hypothesis=None, ranked=[], claims=claims),
        receipts,
        drop_uncited_numbers=True,
    )
    assert dropped == []
    assert "Sports" in digest["headline"]["text"] or "Chiefs" in digest["headline"]["text"]


def test_10_rebuilding_a_day_gives_the_same_digest(db: duckdb.DuckDBPyConnection) -> None:
    first = get_digest(db, DAY)
    again = build_digest(db, DAY, AS_OF)
    assert first is not None
    for k in ("headline", "totals", "cards", "period_days", "windows"):
        assert again[k] == first[k], k


def test_11_too_little_history_says_so(tmp_path: Path) -> None:
    conn = connect(tmp_path / "short.duckdb")
    add_market(conn, "c1", "Sports", "England vs Czechia")
    add_trades(
        conn,
        [{"ts": YESTERDAY + timedelta(hours=h), "wallet": "a", "usd": 50.0} for h in range(24)],
    )
    d = build_digest(conn, DAY, AS_OF)
    conn.close()
    assert d["period_days"] == 0
    shift = card(d, "share_shift")
    assert shift["insufficient"] and "not enough history" in shift["sentence"]["text"]
    assert card(d, "movers")["insufficient"]


def test_12_the_api_serves_stored_digests(store: Path) -> None:
    app = create_app(
        db_path=store,
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        clock=lambda: AS_OF,
    )
    with TestClient(app) as client:
        latest = client.get("/api/digest")
        assert latest.status_code == 200 and latest.json()["day"] == "2026-10-15"
        assert client.get("/api/digest/2026-10-15").json()["day"] == "2026-10-15"
        assert client.get("/api/digests").json() == [{"day": "2026-10-15"}]
        assert client.get("/api/digest/2026-01-01").status_code == 404
