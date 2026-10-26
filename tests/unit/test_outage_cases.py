"""Cases whose alarm fired while our record was far behind (the collector was down) were judged
on missing data. They stay as judged (rule 2) but are flagged, and the Now page's latest
investigations leave them out (user decision 2026-10-23)."""

from __future__ import annotations

from datetime import datetime, timedelta

from gridcase.ledger.schema import Receipt
from gridcase.memory.cases import CaseRecord
from gridcase.memory.retractions import judged_without_data
from gridcase.server.app import _case_summary, now_cases

AS_OF = datetime(2026, 10, 21, 1, 45)


def _coverage(as_of: datetime, freshness: float | None) -> Receipt:
    return Receipt(
        receipt_id=f"cov-{as_of:%H%M}-{freshness}",
        tool="data.coverage",
        inputs={},
        as_of=as_of,
        data_version=None,
        output={"freshness_minutes": freshness, "complete": 0},
        effect_size=None,
        runtime_ms=1,
    )


def _case(case_id: str, *receipts: Receipt, as_of: datetime = AS_OF) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        entity="all",
        metric="volume_usd",
        opened_at=as_of,
        as_of=as_of,
        state="closed",
        season="x",
        receipts=list(receipts),
    )


def test_stale_record_at_opening_marks_the_case() -> None:
    outage = _case("outage", _coverage(AS_OF, 2086.3), _coverage(AS_OF + timedelta(days=1), 1.5))
    assert judged_without_data(outage)
    assert _case_summary(outage)["judged_without_data"] is True


def test_fresh_at_opening_is_not_marked_even_if_the_recheck_was_stale() -> None:
    # the 2026-10-19 case: fresh when it opened, stale only on the next-day recheck
    ok = _case("ok", _coverage(AS_OF, 0.2), _coverage(AS_OF + timedelta(days=1), 1337.3))
    assert not judged_without_data(ok)
    assert not judged_without_data(_case("no-coverage"))
    assert not judged_without_data(_case("unknown", _coverage(AS_OF, None)))


def test_now_page_lists_only_cases_judged_on_data() -> None:
    cases = [
        _case("new", _coverage(AS_OF + timedelta(days=2), 0.8), as_of=AS_OF + timedelta(days=2)),
        *[_case(f"outage-{i}", _coverage(AS_OF, 2086.3)) for i in range(13)],
        *[
            _case(
                f"old-{i}",
                _coverage(AS_OF - timedelta(days=i + 1), 2.0),
                as_of=AS_OF - timedelta(days=i + 1),
            )
            for i in range(12)
        ],
    ]
    listed = now_cases(cases)
    assert len(listed) == 10
    assert [c["case_id"] for c in listed[:2]] == ["new", "old-0"]
    assert not any(c["case_id"].startswith("outage") for c in listed)
