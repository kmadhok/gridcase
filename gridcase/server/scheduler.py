"""Drain scheduled re-checks: every `scheduled_tasks` row whose run_at has passed re-runs
its case at that as_of and closes it. Deterministic; no model calls."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import duckdb

from gridcase.investigate import recheck

log = logging.getLogger("gridcase")


def drain_scheduled(conn: duckdb.DuckDBPyConnection, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    rows = conn.execute(
        "SELECT task_id, case_id, run_at FROM scheduled_tasks WHERE status = 'scheduled' "
        "AND run_at <= ? ORDER BY run_at",
        [now],
    ).fetchall()
    done: list[str] = []
    for task_id, case_id, run_at in rows:
        try:
            recheck(conn, case_id, as_of=run_at)
            conn.execute("UPDATE scheduled_tasks SET status = 'done' WHERE task_id = ?", [task_id])
            done.append(case_id)
        except Exception as e:  # noqa: BLE001 - a failed re-check must not stop the others
            log.warning("recheck of %s failed: %s", case_id, e)
            conn.execute(
                "UPDATE scheduled_tasks SET status = 'failed' WHERE task_id = ?", [task_id]
            )
    return done
