"""Hourly warehouse build and publication, isolated from the gridcase Python environment."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

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
    check_sources = BashOperator(
        task_id="check_sources",
        bash_command='cd "$GRIDCASE_ROOT" && "$UV" run gridcase warehouse check-sources',
        env=task_env,
        append_env=True,
    )
    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command='cd "$GRIDCASE_ROOT" && "$UV" run gridcase dbt build',
        env=task_env,
        append_env=True,
    )
    dbt_docs = BashOperator(
        task_id="dbt_docs",
        bash_command='cd "$GRIDCASE_ROOT" && "$UV" run gridcase dbt docs generate',
        env=task_env,
        append_env=True,
    )
    publish_marts = BashOperator(
        task_id="publish_marts",
        bash_command='cd "$GRIDCASE_ROOT" && "$UV" run gridcase warehouse publish',
        env=task_env,
        append_env=True,
    )

    check_sources >> dbt_build >> dbt_docs >> publish_marts
