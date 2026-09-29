"""publish_case: write the case file and schedule a re-check a day later."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from gridcase.config import DATA_DIR
from gridcase.memory.cases import CaseRecord

ALLOWED_ACTIONS = {"publish_case"}
CASES_DIR = DATA_DIR / "cases"


RECHECK_AFTER = timedelta(hours=24)


def recheck_delay(case: CaseRecord) -> timedelta:
    """Re-run the evidence a day later: did the change persist, and does the answer hold?"""
    return RECHECK_AFTER


def publish_case(
    conn: duckdb.DuckDBPyConnection,
    case: CaseRecord,
    dry_run: bool = True,
    cases_dir: Path | None = None,
) -> dict[str, Any]:
    action = "publish_case"
    if action not in ALLOWED_ACTIONS:
        raise PermissionError(action)
    run_at = case.as_of + recheck_delay(case)
    task_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO scheduled_tasks (task_id, case_id, kind, run_at, status, created_at) "
        "VALUES (?, ?, 'recheck', ?, 'scheduled', ?)",
        [task_id, case.case_id, run_at, datetime.now(UTC).replace(tzinfo=None, microsecond=0)],
    )
    path: str | None = None
    if not dry_run:
        d = cases_dir or CASES_DIR
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{case.case_id}.json"
        p.write_text(case.model_dump_json(indent=2))
        path = str(p)
    return {
        "action": action,
        "dry_run": dry_run,
        "path": path,
        "recheck_task_id": task_id,
        "recheck_at": run_at.isoformat(),
    }
