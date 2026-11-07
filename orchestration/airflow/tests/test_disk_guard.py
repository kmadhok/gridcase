"""Only synthetic pulse files and injected wall clocks; no warehouse commands are run."""

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from unittest.mock import Mock

import pytest
from airflow.sdk import Context
from airflow.sdk.exceptions import AirflowSkipException

SPEC = importlib.util.spec_from_file_location(
    "marketcase_warehouse", Path(__file__).resolve().parents[1] / "dags/marketcase_warehouse.py"
)
assert SPEC is not None and SPEC.loader is not None
warehouse = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = warehouse
SPEC.loader.exec_module(warehouse)

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARKETCASE_PULSE_LOG", raising=False)
    monkeypatch.delenv("MARKETCASE_DISK_GUARD_MODE", raising=False)


@pytest.fixture()
def pulse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pulse.log"
    monkeypatch.setenv("MARKETCASE_PULSE_LOG", str(path))
    return path


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    current = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return current[0]

    monkeypatch.setattr(warehouse, "datetime", Clock)
    return current


def record(free: object = 25, timestamp: str | None = None) -> str:
    return json.dumps({"ts": timestamp or NOW.isoformat(), "c_free_gb": free}) + "\n"


@pytest.mark.parametrize("free", [25, 25.001, 100])
@pytest.mark.parametrize("age", [timedelta(0), timedelta(minutes=30)])
def test_thresholds_pass(pulse: Path, free: float, age: timedelta) -> None:
    pulse.write_text(record(free, (NOW - age).isoformat()))
    warehouse.check_host_disk(now=NOW)


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (record(24.999), "below 25"),
        (record(0), "below 25"),
        (record(-1), "invalid c_free_gb"),
        (record(None), "invalid c_free_gb"),
        (record(True), "invalid c_free_gb"),
        (record("25"), "invalid c_free_gb"),
        (record(float("nan")), "invalid c_free_gb"),
        (record(float("inf")), "invalid c_free_gb"),
        (record(25, (NOW + timedelta(microseconds=1)).isoformat()), "future"),
        (record(25, (NOW - timedelta(minutes=30, microseconds=1)).isoformat()), "older than 30"),
        (record(25, "2026-11-07T12:00:00"), "timezone"),
        (record(25, "not-a-date"), "timestamp"),
        ('{"ts": "2026-11-07T12:00:00Z"}\n', "c_free_gb"),
        ('{"c_free_gb": 25}\n', "timestamp"),
        ('{"ts": "2026-11-07T12:00:00Z", "c_free_gb": 0, "c_free_gb": 25}\n', "duplicate"),
        ('["2026-11-07T12:00:00Z", 25]\n', "malformed"),
        ("not a pulse record\n", "malformed"),
        ("", "empty"),
        ("\n \n", "empty"),
    ],
)
def test_invalid_evidence_skips(pulse: Path, entry: str, reason: str) -> None:
    pulse.write_text(entry)
    with pytest.raises(AirflowSkipException, match=reason):
        warehouse.check_host_disk(now=NOW)


@pytest.mark.parametrize("mode", [None, "guarded", "", "typo"])
def test_missing_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    if mode is not None:
        monkeypatch.setenv("MARKETCASE_DISK_GUARD_MODE", mode)
    with pytest.raises(AirflowSkipException, match="MARKETCASE_"):
        warehouse.check_host_disk(now=NOW)


def test_development_is_an_explicit_bypass(monkeypatch: pytest.MonkeyPatch, pulse: Path) -> None:
    # Even an unreadable/missing file is ignored only when development is explicitly selected.
    monkeypatch.setenv("MARKETCASE_DISK_GUARD_MODE", "development")
    warehouse.check_host_disk(now=NOW)


@pytest.mark.parametrize("path", ["", "   ", "relative/pulse.log"])
def test_invalid_path_skips(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setenv("MARKETCASE_PULSE_LOG", path)
    with pytest.raises(AirflowSkipException, match="MARKETCASE_PULSE_LOG"):
        warehouse.check_host_disk(now=NOW)


def test_missing_file_skips(pulse: Path) -> None:
    with pytest.raises(AirflowSkipException, match="cannot read"):
        warehouse.check_host_disk(now=NOW)


def test_non_file_skips(pulse: Path) -> None:
    pulse.mkdir()
    with pytest.raises(AirflowSkipException, match="regular file"):
        warehouse.check_host_disk(now=NOW)


@pytest.mark.parametrize(
    "last", [record(24), record(25, "bad-date"), "broken\n", '{"ts": "2026-11-07T12:00:00Z"']
)
def test_latest_record_must_pass_no_fallback(pulse: Path, last: str) -> None:
    pulse.write_text(record(100) + last)
    with pytest.raises(AirflowSkipException):
        warehouse.check_host_disk(now=NOW)


@pytest.mark.parametrize(
    "line",
    [
        "2026-11-07T12:00:00Z host=kayak c_free_gb=25 wsl_mem_gb=8\n",
        "2026-11-07T06:00:00-06:00 c_free_gb=25.0\n\n",
        "2026-11-07T12:00:00Z c_free_gb=2.5e1\n",
        '{"timestamp": "2026-11-07T12:00:00Z", "c_free_gb": 25}\n',
    ],
)
def test_supported_pulse_formats(pulse: Path, line: str) -> None:
    pulse.write_text(line)
    warehouse.check_host_disk(now=NOW)


@pytest.mark.parametrize(
    "line",
    [
        "2026-11-07T12:00:00Z c_free_gb=25GB\n",
        "2026-11-07T12:00:00Z c_free_gb=2_5\n",
        "2026-11-07T12:00:00Z c_free_gb=nan\n",
        "2026-11-07T12:00:00Z c_free_gb=inf\n",
        "2026-11-07T12:00:00Z c_free_gb=0 c_free_gb=25\n",
        "2026-11-07T12:00:00Z other_c_free_gb=25\n",
    ],
)
def test_malformed_text_metrics_skip(pulse: Path, line: str) -> None:
    pulse.write_text(line)
    with pytest.raises(AirflowSkipException, match="c_free_gb"):
        warehouse.check_host_disk(now=NOW)


def test_tail_does_not_decode_old_bytes(pulse: Path) -> None:
    pulse.write_bytes(b"\xff" * (2 * warehouse.PULSE_TAIL_BYTES) + b"\n" + record().encode())
    warehouse.check_host_disk(now=NOW)


def test_tail_io_is_bounded(pulse: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pulse.write_bytes(b"x" * (2 * warehouse.PULSE_TAIL_BYTES) + b"\n" + record().encode())
    read = Mock(wraps=os.read)
    monkeypatch.setattr(warehouse.os, "read", read)
    warehouse.check_host_disk(now=NOW)
    assert read.call_count > 0
    assert sum(call.args[1] for call in read.call_args_list) <= warehouse.PULSE_TAIL_BYTES + 1


def test_record_at_tail_boundary_is_kept(pulse: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(warehouse, "PULSE_TAIL_BYTES", len(record().encode()))
    pulse.write_text("old history\n" + record())
    warehouse.check_host_disk(now=NOW)


def test_short_read_skips(pulse: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pulse.write_text(record())
    read = os.read
    monkeypatch.setattr(warehouse.os, "read", lambda fd, size: read(fd, size)[:-1])
    with pytest.raises(AirflowSkipException, match="reliable tail"):
        warehouse.check_host_disk(now=NOW)


def test_unreadable_file_skips(pulse: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pulse.write_text(record())
    monkeypatch.setattr(warehouse.os, "open", Mock(side_effect=PermissionError(13, "denied")))
    with pytest.raises(AirflowSkipException, match="cannot read.*denied"):
        warehouse.check_host_disk(now=NOW)


def test_fifo_path_skips_without_waiting_for_a_writer(pulse: Path) -> None:
    os.mkfifo(pulse)
    with pytest.raises(AirflowSkipException, match="regular file"):
        warehouse.check_host_disk(now=NOW)


@pytest.mark.parametrize(
    "entry",
    [record().rstrip("\n"), "2026-11-07T12:00:00Z c_free_gb=25", '{"ts": "2026-11-07T12'],
)
def test_incomplete_final_line_skips(pulse: Path, entry: str) -> None:
    pulse.write_text(record(100) + entry)
    with pytest.raises(AirflowSkipException, match="incomplete"):
        warehouse.check_host_disk(now=NOW)


def test_record_outside_tail_cannot_authorize_build(pulse: Path) -> None:
    pulse.write_text(record(100) + "x" * (2 * warehouse.PULSE_TAIL_BYTES))
    with pytest.raises(AirflowSkipException, match="tail"):
        warehouse.check_host_disk(now=NOW)


def test_invalid_utf8_skips(pulse: Path) -> None:
    pulse.write_bytes(record().encode() + b"\xff\n")
    with pytest.raises(AirflowSkipException, match="UTF-8"):
        warehouse.check_host_disk(now=NOW)


def test_runtime_clock_and_environment_are_used(
    pulse: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pulse.write_text(record())
    warehouse.check_host_disk(now=NOW)
    with pytest.raises(AirflowSkipException, match="older than 30"):
        warehouse.check_host_disk(now=NOW + timedelta(minutes=31))
    monkeypatch.delenv("MARKETCASE_PULSE_LOG")
    with pytest.raises(AirflowSkipException, match="MARKETCASE_PULSE_LOG"):
        warehouse.check_host_disk(now=NOW)


@pytest.mark.parametrize("task_id", ["check_sources", "dbt_build", "dbt_docs", "publish_marts"])
def test_commands_recheck_before_every_attempt(
    pulse: Path, clock: list[datetime], monkeypatch: pytest.MonkeyPatch, task_id: str
) -> None:
    command = Mock(return_value="test-only command")
    monkeypatch.setattr(warehouse.BashOperator, "execute", command)
    task = warehouse.dag.get_task(task_id)
    pulse.write_text(record())
    warehouse.check_host_disk()
    clock[0] += timedelta(minutes=31)
    with pytest.raises(AirflowSkipException, match="older than 30"):
        task.execute(context={})
    command.assert_not_called()
    pulse.write_text(record(25, clock[0].isoformat()))
    assert task.execute(context={}) == "test-only command"
    assert command.call_count == 1
    pulse.write_text(record(24, clock[0].isoformat()))
    with pytest.raises(AirflowSkipException, match="below 25"):
        task.execute(context={})
    assert command.call_count == 1


@pytest.mark.parametrize("free", [None, 24.999, 25])
def test_airflow_propagates_skip_through_warehouse_chain(
    airflow_db: None,
    pulse: Path,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
    free: float | None,
) -> None:
    if free is not None:
        pulse.write_text(record(free))
    commands = Mock(return_value="test-only command")
    monkeypatch.setattr(warehouse.BashOperator, "execute", commands)
    for task in warehouse.dag.tasks:
        monkeypatch.setattr(task, "retries", 0)
    run = warehouse.dag.test(logical_date=NOW)
    states = {ti.task_id: ti.state for ti in run.get_task_instances()}
    if free == 25:
        assert states == dict.fromkeys(warehouse.dag.task_ids, "success")
        assert commands.call_count == 4
    else:
        assert states == dict.fromkeys(warehouse.dag.task_ids, "skipped")
        commands.assert_not_called()


@pytest.mark.parametrize("change", ["stale", "low"])
def test_airflow_skips_build_when_evidence_changes_after_sources(
    airflow_db: None,
    pulse: Path,
    clock: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    pulse.write_text(record())
    executed: list[str] = []

    def command(context: Context) -> str:
        executed.append(context["task"].task_id)
        if change == "stale":
            clock[0] += timedelta(minutes=31)
        else:
            pulse.write_text(record(24))
        return "test-only command"

    monkeypatch.setattr(warehouse.BashOperator, "execute", staticmethod(command))
    for task in warehouse.dag.tasks:
        monkeypatch.setattr(task, "retries", 0)
    run = warehouse.dag.test(logical_date=NOW)
    assert {ti.task_id: ti.state for ti in run.get_task_instances()} == {
        "check_host_disk": "success",
        "check_sources": "success",
        "dbt_build": "skipped",
        "dbt_docs": "skipped",
        "publish_marts": "skipped",
    }
    assert executed == ["check_sources"]
