"""Run in the isolated Airflow environment; importing the DAG needs no gridcase package."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.models import DagBag


def test_marketcase_warehouse_dag() -> None:
    bag = DagBag(dag_folder=str(Path(__file__).resolve().parents[1] / "dags"))
    assert not bag.import_errors
    dag = bag.dags["marketcase_warehouse"]
    chain = ["check_host_disk", "check_sources", "dbt_build", "dbt_docs", "publish_marts"]
    assert set(dag.task_ids) == set(chain)
    for i, task_id in enumerate(chain):
        task = dag.get_task(task_id)
        assert task.upstream_task_ids == ({chain[i - 1]} if i else set())
        assert task.downstream_task_ids == ({chain[i + 1]} if i < len(chain) - 1 else set())
        assert task.retries == 2
        assert task.retry_delay == timedelta(minutes=5)
        assert task.execution_timeout == timedelta(minutes=30)
        assert task.trigger_rule == "all_success"
        if task_id != "check_host_disk":
            assert task.append_env is True
    assert dag.schedule == "20 * * * *"
    assert dag.start_date == datetime(2026, 10, 1, tzinfo=UTC)
    assert dag.catchup is False
    assert dag.max_active_runs == 1
