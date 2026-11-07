"""Hourly warehouse build and publication, isolated from the gridcase Python environment."""

import json
import logging
import math
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Context
from airflow.sdk.exceptions import AirflowSkipException

PULSE_TAIL_BYTES = 64 * 1024
MIN_C_FREE_GB = 25
MAX_PULSE_AGE = timedelta(minutes=30)
LOGGER = logging.getLogger(__name__)


def _latest_pulse_line(path: str) -> str:
    """Read a bounded snapshot of a regular log, ignoring a cut-off leading line."""
    # O_NONBLOCK prevents a mistaken FIFO path from hanging the guard on open.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("MARKETCASE_PULSE_LOG must be a regular file")
        start = max(0, info.st_size - PULSE_TAIL_BYTES - 1)
        os.lseek(descriptor, start, os.SEEK_SET)
        tail = os.read(descriptor, PULSE_TAIL_BYTES + 1)
        if len(tail) != info.st_size - start:
            raise ValueError("pulse log changed or could not be fully read; no reliable tail")
    finally:
        os.close(descriptor)
    if start:
        tail = tail.partition(b"\n")[2]
    if tail and not tail.rstrip(b" \t\r").endswith(b"\n"):
        raise ValueError("latest pulse record is incomplete (missing newline)")
    lines = [line for line in tail.split(b"\n") if line.strip()]
    if not lines:
        raise ValueError("pulse log is empty or has no complete record in the bounded tail")
    try:
        return lines[-1].decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("latest pulse record is not valid UTF-8") from exc


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f"duplicate pulse field: {key}")
        fields[key] = value
    return fields


def _parse_pulse(line: str) -> tuple[datetime, float]:
    """Accept JSONL or an ISO timestamp followed by whitespace-separated key=value fields."""
    if line.startswith("{"):
        fields = json.loads(line, object_pairs_hook=_unique_fields)
        timestamp_keys = [key for key in ("ts", "timestamp") if key in fields]
        if len(timestamp_keys) != 1:
            raise ValueError("pulse timestamp must use exactly one of ts or timestamp")
        timestamp = fields[timestamp_keys[0]]
        free = fields.get("c_free_gb")
        if isinstance(free, bool) or not isinstance(free, (int, float)):
            raise ValueError("invalid c_free_gb: expected a number")
        free_gb = float(free)
    else:
        tokens = line.split()
        readings = [
            token.removeprefix("c_free_gb=")
            for token in tokens[1:]
            if token.startswith("c_free_gb=")
        ]
        if len(readings) != 1:
            raise ValueError("malformed pulse: expected exactly one c_free_gb field")
        timestamp = tokens[0]
        if not re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", readings[0]
        ):
            raise ValueError("invalid c_free_gb: expected a decimal number")
        free_gb = float(readings[0])
    if not math.isfinite(free_gb) or free_gb < 0:
        raise ValueError("invalid c_free_gb: expected a finite, nonnegative number")
    if not isinstance(timestamp, str):
        raise ValueError("invalid pulse timestamp: expected an ISO 8601 string")
    try:
        observed_at = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError("invalid pulse timestamp: expected ISO 8601") from exc
    if observed_at.tzinfo is None:
        raise ValueError("pulse timestamp must include a timezone")
    return observed_at.astimezone(UTC), free_gb


def check_host_disk(*, now: datetime | None = None) -> None:
    """Fail closed on absent/unsafe Windows C: evidence; development bypass is explicit."""
    mode = os.environ.get("MARKETCASE_DISK_GUARD_MODE", "guarded")
    if mode == "development":
        LOGGER.warning("Windows disk guard bypassed: MARKETCASE_DISK_GUARD_MODE=development")
        return
    if mode != "guarded":
        raise AirflowSkipException(
            "Windows disk guard: MARKETCASE_DISK_GUARD_MODE must be guarded or development"
        )
    path = os.environ.get("MARKETCASE_PULSE_LOG", "")
    if not path.strip() or not Path(path).is_absolute():
        raise AirflowSkipException(
            "Windows disk guard: guarded mode requires an absolute MARKETCASE_PULSE_LOG path"
        )
    try:
        observed_at, free_gb = _parse_pulse(_latest_pulse_line(path))
    except OSError as exc:
        raise AirflowSkipException(
            f"Windows disk guard: cannot read MARKETCASE_PULSE_LOG ({exc.strerror})"
        ) from exc
    except (ValueError, OverflowError, RecursionError) as exc:
        raise AirflowSkipException(f"Windows disk guard: {exc}") from exc
    age = (now if now is not None else datetime.now(UTC)) - observed_at
    if age < timedelta(0):
        raise AirflowSkipException("Windows disk guard: latest pulse timestamp is in the future")
    if age > MAX_PULSE_AGE:
        raise AirflowSkipException(
            "Windows disk guard: latest pulse is older than 30 minutes "
            f"({age.total_seconds():.1f}s)"
        )
    if free_gb < MIN_C_FREE_GB:
        raise AirflowSkipException(f"Windows disk guard: c_free_gb={free_gb} is below 25 GB")
    LOGGER.info("Windows disk guard passed: c_free_gb=%g, age=%ss", free_gb, age.total_seconds())


class DiskGuardedBashOperator(BashOperator):
    """Recheck at execution time, including retries or individually cleared tasks."""

    def execute(self, context: Context) -> object:
        check_host_disk()
        return super().execute(context)


task_env = {
    "GRIDCASE_ROOT": os.environ.get(
        "GRIDCASE_ROOT", str(Path.home() / "Documents/Claude/Projects/gridcase")
    ),
    "UV": os.environ.get("UV", str(Path.home() / ".local/bin/uv")),
}

with DAG(
    dag_id="marketcase_warehouse",
    schedule="20 * * * *",
    start_date=datetime(2026, 10, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "execution_timeout": timedelta(minutes=30),
    },
) as dag:
    disk_guard = PythonOperator(
        task_id="check_host_disk",
        python_callable=check_host_disk,
    )
    check_sources = DiskGuardedBashOperator(
        task_id="check_sources",
        bash_command='cd "$GRIDCASE_ROOT" && "$UV" run gridcase warehouse check-sources',
        env=task_env,
        append_env=True,
    )
    dbt_build = DiskGuardedBashOperator(
        task_id="dbt_build",
        bash_command='cd "$GRIDCASE_ROOT" && "$UV" run gridcase dbt build',
        env=task_env,
        append_env=True,
    )
    dbt_docs = DiskGuardedBashOperator(
        task_id="dbt_docs",
        bash_command='cd "$GRIDCASE_ROOT" && "$UV" run gridcase dbt docs generate',
        env=task_env,
        append_env=True,
    )
    publish_marts = DiskGuardedBashOperator(
        task_id="publish_marts",
        bash_command='cd "$GRIDCASE_ROOT" && "$UV" run gridcase warehouse publish',
        env=task_env,
        append_env=True,
    )

    disk_guard >> check_sources >> dbt_build >> dbt_docs >> publish_marts
