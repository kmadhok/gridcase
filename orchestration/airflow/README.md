# MarketCase warehouse disk guard

The DAG runs `check_host_disk` → `check_sources` → `dbt_build` → `dbt_docs` →
`publish_marts`. Each task requires its upstream task to succeed. The disk guard
raises `AirflowSkipException` for unsafe or unavailable evidence, so source
checks, the build, docs and publishing also skip rather than retrying a build
against a full Windows disk. Each command task also checks immediately before
execution, including retries and individually cleared tasks, so a pulse that
expires while queued cannot authorize a later build.

## Configuration

- `MARKETCASE_DISK_GUARD_MODE=guarded` is the default, and is explicitly set in
  the kayak Airflow unit. A missing path never disables the guard.
- `MARKETCASE_PULSE_LOG` must be the absolute path to the existing host pulse
  log, readable by the Airflow task process. Configure it in the Airflow service
  environment (for example, a systemd drop-in). The Python task does not load
  the gridcase CLI's `.env`. No live path is guessed or installed by this change.
- `MARKETCASE_DISK_GUARD_MODE=development` explicitly bypasses the disk check
  for local development without a Windows host pulse. The task logs this bypass.
  Do not use this mode on kayak. Empty or unknown modes skip.

The guard reads at most the last 64 KiB plus one boundary byte; it never scans
the whole log. Only the last nonblank record counts. A partial record at the
start of the tail is discarded. Records must end with a newline: a malformed
final record or incomplete append skips; it cannot fall back to an earlier
healthy reading. Short reads (for example, from concurrent truncation) also
skip. File mtime does not establish freshness.

Supported records (other fields may be present):

```text
2026-11-07T12:00:00Z host=kayak c_free_gb=25.0
```

```json
{"ts": "2026-11-07T12:00:00Z", "c_free_gb": 25.0}
```

JSON may use `timestamp` instead of `ts`, but must use exactly one. Timestamps
must be ISO 8601 with an explicit timezone (`Z` or a numeric offset). JSON
`c_free_gb` must be a number, not a string or boolean. Duplicate JSON keys or `c_free_gb` fields,
non-finite/negative values and malformed records are rejected. This is the
host's Windows C: free space, not WSL's apparent filesystem capacity.

A guarded run needs **at least 25 GB** and a reading between **now minus 30
minutes and now**, inclusive, using the task's wall clock in UTC. Future
timestamps, older evidence, empty/missing/unreadable logs, and invalid metrics
all skip with a reason in the task log. This is a preflight check, not a disk
reservation or a guarantee of enough space for a full rebuild. Direct CLI dbt
runs are outside this DAG guard.

## Before re-enabling Airflow on kayak

Keep Airflow disabled until all of these prerequisites are met:

1. Complete and validate the remaining `marketcase-incremental-dbt` work,
   particularly incremental marts. This PR only adds the disk guard; it does
   not make full warehouse rebuilds safe on a shared host.
2. Reconcile the deployed kayak checkout with main and its local commits and
   configuration. The open server-memory PR [#13](https://github.com/kmadhok/gridcase/pull/13)
   is independent: its service/DuckDB limits do not supply this disk guard,
   and spilling still consumes disk. Review its deployment separately.
3. Confirm the actual pulse writer's format, timestamp timezone and cadence,
   configure `MARKETCASE_PULSE_LOG` in the task environment, and verify a fresh
   C: reading against this contract. No live pulse log was inspected here.
4. Reconcile the DAG and installed Airflow unit configuration and validate the
   skip path before an explicitly approved restart/unpause. Installing this
   repository's `deploy/install.sh` enables Airflow, and the unit still sets
   `DAGS_ARE_PAUSED_AT_CREATION=False`; do not run it as a way to stage this
   change while the prerequisites are outstanding. Previously installed units
   and DAGs do not change merely because a PR merges.

No service, live checkout, log, database or deployment is changed by the tests.

## Verification

Use the isolated Airflow environment, keeping all metadata and test logs local:

```bash
bash orchestration/airflow/setup.sh
orchestration/airflow/.venv/bin/pytest orchestration/airflow/tests -q \
  --basetemp="$PWD/.pytest_cache/airflow-tmp"
uv run pytest tests/exit/test_m10.py tests/unit/test_deploy_units.py -q \
  --basetemp="$PWD/.pytest_cache/project-tmp"
uv run ruff check .
uv run mypy --strict
```

Disk evidence tests use temporary synthetic files and fixed clocks; propagation
tests replace warehouse commands with harmless test tasks.
