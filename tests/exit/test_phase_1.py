"""Phase 1 exit test: bitemporal store and idempotent, fail-soft ingestion.

Offline checks run against a temporary DuckDB file and fixture EIA responses served by an
httpx.MockTransport. The full 2018→today backfill check is marked `live` and only runs with
GRIDCASE_LIVE=1 (intended for the VPS, not the laptop loop).

All timestamps are naive UTC (see docs/DECISIONS.md, Phase 1).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gridcase.ingest.eia import EiaSource
from gridcase.ingest.runner import run_ingest
from gridcase.store.db import append_observations, as_of, connect, one
from gridcase.store.models import Observation

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eia"
FIX_DAY = datetime(2021, 2, 15)
REGIONS = ["ERCO", "CISO", "PJM", "US48"]


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    conn = connect(tmp_path / "t.duckdb")
    yield conn
    conn.close()


def _fixture_transport(fail_first: int = 0, always_fail: bool = False) -> httpx.MockTransport:
    """Serve cached EIA responses from tests/fixtures/eia; optionally 429 first N calls."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if always_fail or calls["n"] <= fail_first:
            return httpx.Response(429, json={"error": "rate limited"})
        route = request.url.path.rstrip("/").split("/")[-2]  # .../{route}/data
        typ = request.url.params.get("facets[type][]", "")
        path = FIXTURES / f"{route}{'_' + typ if typ else ''}.json"
        if not path.exists():
            return httpx.Response(200, json={"response": {"total": 0, "data": []}})
        return httpx.Response(200, content=path.read_bytes())

    return httpx.MockTransport(handler)


def _client(**kw: object) -> httpx.Client:
    return httpx.Client(transport=_fixture_transport(**kw), base_url="https://api.eia.gov")  # type: ignore[arg-type]


def _ingest_fixture_day(db: duckdb.DuckDBPyConnection, **kw: object) -> str:
    src = EiaSource(client=_client(**kw), api_key="test", raw_dir=None, retry_sleep=0.0)
    return run_ingest(db, src, start=FIX_DAY, end=FIX_DAY + timedelta(days=1), mode="backfill")


def _count(db: duckdb.DuckDBPyConnection) -> int:
    return int(one(db, "SELECT count(*) FROM observations")[0])


# --------------------------------------------------------------------------- 1. live backfill


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GRIDCASE_LIVE"), reason="set GRIDCASE_LIVE=1 on the VPS")
def test_1_full_backfill_row_counts() -> None:
    """Backfill 2018-07-01→today for the four regions; hourly D/DF/NG/TI within 3% of expected."""
    from gridcase.config import DB_PATH

    conn = connect(DB_PATH, read_only=True)
    now = datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    for region in REGIONS:
        for series in ("eia.D", "eia.DF", "eia.NG", "eia.TI"):
            first, n = one(
                conn,
                "SELECT min(valid_time), count(DISTINCT valid_time) FROM observations "
                "WHERE series_id = ? AND entity_id = ?",
                [series, region],
            )
            assert first is not None, f"{region}/{series}: no rows"
            expected = int((now - first).total_seconds() // 3600)
            lag_allowance = 48
            assert abs(n - expected) <= max(0.03 * expected, lag_allowance), (
                f"{region}/{series}: {n} hours vs {expected} expected from {first}"
            )


# --------------------------------------------------------------------------- 2. property test


@st.composite
def stores(draw: st.DrawFn) -> list[Observation]:
    """Random append-only histories for a handful of keys."""
    base = datetime(2021, 1, 1)
    rows: list[Observation] = []
    for series, entity in [("s1", "e1"), ("s1", "e2"), ("s2", "e1")]:
        n_valid = draw(st.integers(1, 6))
        for v in range(n_valid):
            n_vers = draw(st.integers(1, 3))
            for k in range(n_vers):
                rows.append(
                    Observation(
                        series_id=series,
                        entity_id=entity,
                        valid_time=base + timedelta(hours=v),
                        value=draw(st.floats(-1e4, 1e4, allow_nan=False)),
                        observed_at=base + timedelta(hours=v + 1 + 24 * k),
                        available_at=base + timedelta(hours=v + 1 + 24 * k),
                        source="test",
                        vintage="observed",
                    )
                )
    return rows


@given(rows=stores(), h1=st.integers(0, 80), h2=st.integers(0, 80))
@settings(max_examples=60, deadline=None)
def test_2_as_of_never_leaks_and_is_monotone(
    tmp_path_factory: pytest.TempPathFactory, rows: list[Observation], h1: int, h2: int
) -> None:
    conn = connect(tmp_path_factory.mktemp("prop") / "p.duckdb")
    try:
        append_observations(conn, rows, dedupe_unchanged=False)
        t1, t2 = sorted(
            [datetime(2021, 1, 1) + timedelta(hours=h1), datetime(2021, 1, 1) + timedelta(hours=h2)]
        )
        a1 = as_of(conn, t1)
        a2 = as_of(conn, t2)
        assert all(r.available_at <= t1 for r in a1)
        assert all(r.available_at <= t2 for r in a2)
        keys1 = {(r.series_id, r.entity_id, r.valid_time) for r in a1}
        keys2 = {(r.series_id, r.entity_id, r.valid_time) for r in a2}
        assert keys1 <= keys2
        # one row per key
        assert len(keys1) == len(a1) and len(keys2) == len(a2)
    finally:
        conn.close()


# --------------------------------------------------------------------------- 3. idempotency


def test_3_rerun_writes_no_duplicates(db: duckdb.DuckDBPyConnection) -> None:
    run1 = _ingest_fixture_day(db)
    n1 = _count(db)
    assert n1 > 0
    run2 = _ingest_fixture_day(db)
    n2 = _count(db)
    assert n2 == n1, "rerun must append nothing when values are unchanged"
    dupes = one(
        db,
        "SELECT count(*) FROM (SELECT series_id, entity_id, valid_time, observed_at, count(*) c "
        "FROM observations GROUP BY ALL HAVING c > 1)",
    )[0]
    assert dupes == 0
    status = db.execute(
        "SELECT run_id, status, rows_written FROM ingest_runs ORDER BY started_at"
    ).fetchall()
    assert [s[1] for s in status] == ["ok", "ok"]
    assert status[0][2] == n1 and status[1][2] == 0
    assert run1 != run2


def test_3b_fixture_day_is_complete(db: duckdb.DuckDBPyConnection) -> None:
    _ingest_fixture_day(db)
    for series in ("eia.D", "eia.DF", "eia.NG", "eia.TI"):
        n = one(
            db,
            "SELECT count(*) FROM observations WHERE series_id = ? AND entity_id = 'ERCO'",
            [series],
        )[0]
        assert n == 24, f"{series}: {n} rows"
    # backfilled rows carry a synthetic vintage with available_at = valid_time + lag
    row = one(
        db,
        "SELECT vintage, valid_time, available_at FROM observations "
        "WHERE series_id = 'eia.D' AND entity_id = 'ERCO' ORDER BY valid_time LIMIT 1",
    )
    assert row[0] == "synthetic"
    assert row[2] == row[1] + timedelta(hours=1)


# --------------------------------------------------------------------------- 4. revisions


def test_4_synthetic_revision_is_bitemporal(db: duckdb.DuckDBPyConnection) -> None:
    _ingest_fixture_day(db)
    t = FIX_DAY + timedelta(hours=5)
    key_sql = "series_id = 'eia.D' AND entity_id = 'ERCO' AND valid_time = ?"
    before = float(one(db, f"SELECT value FROM observations WHERE {key_sql}", [t])[0])
    later = datetime(2021, 3, 1)
    append_observations(
        db,
        [
            Observation(
                series_id="eia.D",
                entity_id="ERCO",
                valid_time=t,
                value=before + 1000.0,
                observed_at=later,
                available_at=later,
                source="test",
                vintage="observed",
            )
        ],
    )

    def val(at: datetime) -> float:
        rows = [r for r in as_of(db, at, series_id="eia.D", entity_id="ERCO") if r.valid_time == t]
        assert len(rows) == 1
        return rows[0].value

    assert val(later - timedelta(hours=1)) == before
    assert val(later) == before + 1000.0
    assert _count(db) > 0
    # the old row is still there: append-only
    n_versions = one(db, f"SELECT count(*) FROM observations WHERE {key_sql}", [t])[0]
    assert n_versions == 2


# --------------------------------------------------------------------------- 5. fail soft


def test_5_429_retries_with_backoff_then_succeeds(
    db: duckdb.DuckDBPyConnection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING", logger="gridcase")
    _ingest_fixture_day(db, fail_first=2)
    assert _count(db) > 0
    status = db.execute("SELECT status FROM ingest_runs").fetchall()
    assert status == [("ok",)]
    assert sum("429" in r.message for r in caplog.records) >= 2


def test_5b_persistent_429_fails_soft_without_corruption(db: duckdb.DuckDBPyConnection) -> None:
    _ingest_fixture_day(db)
    n_before = _count(db)
    _ingest_fixture_day(db, always_fail=True)  # must not raise
    assert _count(db) == n_before
    runs = db.execute("SELECT status, error FROM ingest_runs ORDER BY started_at").fetchall()
    assert runs[0][0] == "ok"
    assert runs[1][0] == "failed" and "429" in (runs[1][1] or "")


# --------------------------------------------------------------------------- fixture sanity


def test_fixtures_present() -> None:
    for name in ("region-data_D", "region-data_DF", "region-data_NG", "region-data_TI"):
        p = FIXTURES / f"{name}.json"
        assert p.exists(), p
        body = json.loads(p.read_text())
        assert len(body["response"]["data"]) == 24
