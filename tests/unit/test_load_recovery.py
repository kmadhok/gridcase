"""A skipped raw hour stays unknown, and late/retried loads replace cached values."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import duckdb
import httpx
import pytest

from gridcase import market_alarm as alarm
from gridcase.ingest import polymarket_load as loader
from gridcase.store.db import connect

HOUR = datetime(2026, 10, 10, 11)
AS_OF = HOUR + timedelta(hours=4, minutes=30)
MARK = HOUR + timedelta(hours=3)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    alarm.clear_cache()
    conn = connect(tmp_path / "store.duckdb")
    yield conn
    conn.close()
    alarm.clear_cache()


def raw(root: Path, hour: datetime) -> Path:
    path = root / "trades" / f"{hour:%Y/%m/%d/%H}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        f.write(json.dumps({
            "proxyWallet": "w", "side": "BUY", "asset": "a", "conditionId": "c",
            "size": 10, "price": 0.5, "timestamp": int(hour.replace(tzinfo=UTC).timestamp()) + 300,
            "title": "q", "slug": "q", "eventSlug": "e", "outcome": "Yes",
            "transactionHash": hour.isoformat(),
            "collected_at": (hour + timedelta(minutes=6)).replace(tzinfo=UTC).isoformat(),
        }) + "\n")  # fmt: skip
    return path


def collector_mark(root: Path, mark: datetime = MARK) -> None:
    (root / "collector_state.json").write_text(
        json.dumps({"complete_to": int(mark.replace(tzinfo=UTC).timestamp())})
    )


def latest_mark(db: duckdb.DuckDBPyConnection) -> datetime | None:
    row = db.execute(
        "SELECT complete_to FROM collector_marks ORDER BY loaded_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def values(db: duckdb.DuckDBPyConnection) -> alarm.Hourly:
    return alarm.hourly(db, alarm.WatchedSeries(kpi="volume_usd"), AS_OF, alarm.WatchParams(), 5)


def test_forget_from_tolerates_keys_vanishing(monkeypatch: pytest.MonkeyPatch) -> None:
    class ClearingCache(dict[tuple[str, str, str, datetime], float | None]):
        def __iter__(self) -> Iterator[tuple[str, str, str, datetime]]:
            keys = list(super().__iter__())
            self.clear()  # another refresh cleared these after iteration took its snapshot
            return iter(keys)

    cache = ClearingCache({("store", "volume_usd", "all", HOUR): 5.0})
    monkeypatch.setattr(alarm, "_cache", cache)
    alarm.forget_from(HOUR)
    assert cache == {}


@pytest.mark.parametrize("fails_first", [True, False])
def test_middle_hour_retry_or_late_load_replaces_cached_zero(
    db: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fails_first: bool,
) -> None:
    for offset in (-1, 1, 2):
        raw(tmp_path, HOUR + timedelta(hours=offset))
    collector_mark(tmp_path)
    load_raw = loader.load_raw
    monkeypatch.setattr(loader, "load_raw", partial(load_raw, now=AS_OF - timedelta(minutes=2)))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])),
        base_url="https://gamma.test",
    ) as client:
        run = loader.refresher(tmp_path, client)
        run(db)
        assert next(r for r in values(db).rows if r["t"] == HOUR)["v"] == 0
        assert any(k[3] == HOUR for k in alarm._cache)
        middle = raw(tmp_path, HOUR)
        monkeypatch.setattr(loader, "load_raw", partial(load_raw, now=AS_OF - timedelta(minutes=1)))
        if fails_first:
            with monkeypatch.context() as patch:
                original = loader._load_file

                def fail(
                    conn: duckdb.DuckDBPyConnection, path: Path
                ) -> tuple[int, tuple[datetime, datetime] | None]:
                    if path == middle:
                        raise duckdb.OutOfMemoryException("injected spill cap")
                    return original(conn, path)

                patch.setattr(loader, "_load_file", fail)
                run(db)
            assert latest_mark(db) == HOUR  # lower than the previously recorded mark
            row = next(r for r in values(db).rows if r["t"] == HOUR)
            assert row["v"] is None and row["z"] is None
            assert not any(k[3] >= HOUR for k in alarm._cache)
            assert db.execute(
                "SELECT count(*) FROM raw_files WHERE path = ?", [str(middle)]
            ).fetchone() == (0,)
        monkeypatch.setattr(loader, "load_raw", partial(load_raw, now=AS_OF))
        run(db)  # no clear_cache: the retry must repair the settled value itself
        assert latest_mark(db) == MARK
        assert next(r for r in values(db).rows if r["t"] == HOUR)["v"] == 5


@pytest.mark.parametrize("mark", [HOUR - timedelta(hours=1), MARK])
def test_failed_hours_bound_the_collector_mark_and_report_the_earliest(
    db: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mark: datetime,
) -> None:
    collector_mark(tmp_path, mark)
    raw(tmp_path, HOUR)
    raw(tmp_path, HOUR + timedelta(hours=1))

    def fail(*args: object) -> None:
        raise duckdb.OutOfMemoryException("injected")

    monkeypatch.setattr(loader, "_load_file", fail)
    result = loader.load_raw(db, tmp_path, now=AS_OF)
    assert latest_mark(db) == min(mark, HOUR)
    assert result.failed_from == HOUR


@pytest.mark.parametrize("prior_mark", [None, HOUR - timedelta(hours=1)])
def test_unparseable_failed_path_records_no_new_mark(
    db: duckdb.DuckDBPyConnection,
    tmp_path: Path,
    prior_mark: datetime | None,
) -> None:
    collector_mark(tmp_path)
    if prior_mark is not None:
        db.execute("INSERT INTO collector_marks VALUES (?, ?)", [prior_mark, HOUR])
    raw(tmp_path, HOUR).write_bytes(b"bad gzip")
    (tmp_path / "trades" / "unparseable.jsonl.gz").write_bytes(b"bad gzip")
    result = loader.load_raw(db, tmp_path, now=AS_OF)
    assert latest_mark(db) == prior_mark
    assert result.failed_from == HOUR


def test_forget_from_evicts_all_stores_and_series_but_preserves_earlier_hours() -> None:
    alarm.clear_cache()
    before = HOUR - timedelta(hours=1)
    for store in ("one", "two"):
        for series in ("trades", "volume_usd"):
            for hour in (before, HOUR, HOUR + timedelta(hours=1)):
                alarm._cache[(store, series, "all", hour)] = 1
    try:
        alarm.forget_from(HOUR)
        assert len(alarm._cache) == 4
        assert all(k[3] == before for k in alarm._cache)
    finally:
        alarm.clear_cache()
