"""Run a source over a window, stamp vintages, append to the store, record the run.

Each chunk is appended as soon as it is fetched, so a failure part-way leaves earlier
chunks committed and the run marked 'failed' with the error; rerunning is safe because
append_observations skips unchanged values.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import duckdb

from gridcase.ingest.base import IngestError, Source
from gridcase.store.db import append_observations
from gridcase.store.models import Observation, RawObs
from gridcase.store.series import SERIES_BY_ID

log = logging.getLogger("gridcase")

Mode = Literal["backfill", "live"]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def stamp(raw: RawObs, source: str, observed_at: datetime, mode: Mode) -> Observation:
    """Backfilled rows get a synthetic available_at = valid_time + lag (never after observed_at)."""
    if mode == "live":
        return Observation(
            **raw.model_dump(), observed_at=observed_at, available_at=observed_at, source=source
        )
    lag = SERIES_BY_ID[raw.series_id].lag_hours
    available = min(raw.valid_time + timedelta(hours=lag), observed_at)
    return Observation(
        **raw.model_dump(),
        observed_at=observed_at,
        available_at=available,
        source=source,
        vintage="synthetic",
    )


def run_ingest(
    conn: duckdb.DuckDBPyConnection,
    source: Source,
    start: datetime,
    end: datetime,
    mode: Mode = "backfill",
) -> str:
    run_id = uuid.uuid4().hex[:12]
    started = _now()
    conn.execute(
        "INSERT INTO ingest_runs (run_id, source, started_at, status) VALUES (?, ?, ?, 'running')",
        [run_id, source.name, started],
    )
    written = 0
    status = "ok"
    error: str | None = None
    try:
        for c0, c1 in source.chunks(start, end):
            raw = source.fetch(c0, c1)
            obs = [stamp(r, source.name, started, mode) for r in raw]
            n = append_observations(conn, obs)
            written += n
            log.info("%s %s..%s: %d fetched, %d new", source.name, c0, c1, len(raw), n)
        partial = getattr(source, "partial_errors", None)
        if partial:
            status = "partial"
            error = "; ".join(partial)[:2000]
    except IngestError as e:
        status, error = "failed", str(e)
        log.error("%s run %s failed: %s", source.name, run_id, e)
    conn.execute(
        "UPDATE ingest_runs SET finished_at = ?, rows_written = ?, status = ?, error = ? "
        "WHERE run_id = ?",
        [_now(), written, status, error, run_id],
    )
    return run_id
