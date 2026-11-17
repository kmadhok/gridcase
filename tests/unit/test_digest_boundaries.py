"""Digest day boundaries and coverage-backed totals copy."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from gridcase.digest import build_digest, due_day, get_digest, totals_sentence
from gridcase.memory.cases import new_case
from gridcase.store.db import connect
from tests.polymarket_synth import START, add_trades


def test_alarm_belongs_to_only_one_digest(market_store: Path) -> None:
    conn = connect(market_store)
    boundary = datetime(2026, 10, 9)
    cases = [
        new_case(conn, "all", "volume_usd", boundary + offset)
        for offset in (timedelta(microseconds=-1), timedelta(), timedelta(minutes=15))
    ]
    found = []
    for day in (date(2026, 10, 9), date(2026, 10, 10)):
        digest = build_digest(
            conn, day, datetime.combine(day, datetime.min.time()) + timedelta(minutes=30)
        )
        alarms = next(c for c in digest["cards"] if c["id"] == "alarms")
        found.append([c["case_id"] for c in alarms["data"]["cases"]])
    assert found == [[cases[0].case_id], [cases[1].case_id, cases[2].case_id]]
    conn.close()


def test_digest_waits_for_latest_visible_collector_mark_then_stores_full_total(
    market_store: Path,
) -> None:
    with connect(market_store) as conn:
        midnight = datetime(2026, 10, 10)
        wall = midnight + timedelta(minutes=30)
        later = wall + timedelta(minutes=2)
        settle = timedelta(minutes=30)
        assert due_day(conn, wall, settle) == midnight.date()  # no marks: legacy behaviour
        conn.executemany(
            "INSERT INTO collector_marks VALUES (?, ?)",
            [
                (midnight, wall - timedelta(minutes=2)),
                (midnight - timedelta(hours=6), wall - timedelta(minutes=1)),
                (midnight, later),  # invisible at wall; latest mark lowers completeness
            ],
        )
        add_trades(
            conn,
            [
                {
                    "ts": midnight - timedelta(hours=3),
                    "wallet": "late",
                    "usd": 50000,
                    "collected_at": later,
                }
            ],
        )
        assert due_day(conn, wall, settle) is None
        assert get_digest(conn, midnight.date()) is None
        assert due_day(conn, later, settle) == midnight.date()
        digest = build_digest(conn, midnight.date(), later)
        lo, hi = [datetime.fromisoformat(t) for t in digest["windows"]["current"]]
        expected = conn.execute(
            "SELECT sum(usd) FROM trades_as_of(?) WHERE ts >= ? AND ts < ?", [later, lo, hi]
        ).fetchone()
        assert expected is not None
        volume = next(t for t in digest["totals"]["items"] if t["kpi"] == "volume_usd")
        assert volume["now"] == pytest.approx(expected[0])
        assert volume["now"] >= 50000
        assert get_digest(conn, midnight.date()) == digest
        assert due_day(conn, later, settle) is None


@pytest.mark.parametrize("missing", [None, "current", "previous", "gap"])
def test_totals_coverage_is_honest_and_cited(market_store: Path, missing: str | None) -> None:
    conn = connect(market_store)
    hour = START + timedelta(days=6 if missing == "current" else 2, hours=5)
    if missing in ("current", "previous"):
        conn.execute(
            "DELETE FROM trades WHERE ts >= ? AND ts < ?", [hour, hour + timedelta(hours=1)]
        )
    elif missing == "gap":
        conn.execute(
            "INSERT INTO collector_gaps VALUES (?, ?, ?)",
            [hour, hour + timedelta(minutes=10), hour + timedelta(hours=1)],
        )
    digest = build_digest(conn, date(2026, 10, 10), datetime(2026, 10, 10, 0, 30))
    sentence = digest["totals"]["sentence"]
    assert digest["headline"] == {**sentence, "card": "totals"}
    if missing is None:
        assert sentence["text"] == totals_sentence(
            digest["totals"], "in the last four days", "the four days before"
        )
        assert "%" in sentence["text"]
    else:
        assert "so we don't compare" in sentence["text"]
        assert "%" not in sentence["text"]
        receipt = next(r for r in digest["receipts"] if r["tool"] == "data.coverage")
        assert sentence["receipt_ids"] == [receipt["receipt_id"]]
        assert "192 hours checked" in sentence["text"]
        assert ("1 hour with no data" if missing != "gap" else "1 collector gap") in sentence[
            "text"
        ]
    conn.close()
