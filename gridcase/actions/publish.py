"""publish_case: write the case file and schedule a re-check when lagged data will have landed."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from gridcase.config import DATA_DIR
from gridcase.memory.cases import CaseRecord
from gridcase.store.series import SERIES_BY_ID

ALLOWED_ACTIONS = {"publish_case"}
CASES_DIR = DATA_DIR / "cases"


def recheck_delay(case: CaseRecord) -> timedelta:
    """now + max(lag_hours of series used), with a 24 h floor."""
    used = {s for r in case.receipts for s in _series_of(r.sql)}
    lags = [SERIES_BY_ID[s].lag_hours for s in used if s in SERIES_BY_ID]
    return timedelta(hours=max([*lags, 24]))


def _series_of(sql_log: list[dict[str, str]]) -> set[str]:
    out: set[str] = set()
    for q in sql_log:
        for s in SERIES_BY_ID:
            if f"series_id = '{s}'" in q["sql"]:
                out.add(s)
    return out


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
        [task_id, case.case_id, run_at, datetime.utcnow().replace(microsecond=0)],
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
