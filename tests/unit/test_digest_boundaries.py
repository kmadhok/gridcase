"""Digest day boundaries and coverage-backed totals copy."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from gridcase.digest import build_digest, totals_sentence
from gridcase.memory.cases import new_case
from gridcase.store.db import connect
from tests.polymarket_synth import START


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
