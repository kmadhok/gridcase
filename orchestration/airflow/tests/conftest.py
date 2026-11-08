"""Keep Airflow configuration, metadata and task logs inside this clone during tests."""

import atexit
import os
import tempfile
from pathlib import Path

import pytest

CACHE = Path(__file__).resolve().parents[3] / ".pytest_cache"
CACHE.mkdir(exist_ok=True)
TEST_HOME = tempfile.TemporaryDirectory(prefix="airflow-tests-", dir=CACHE)
atexit.register(TEST_HOME.cleanup)

# Set before any Airflow imports; never inherit a production metadata connection.
os.environ["AIRFLOW_HOME"] = TEST_HOME.name
os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = f"sqlite:///{TEST_HOME.name}/airflow.db"
os.environ["AIRFLOW__CORE__DAGS_FOLDER"] = str(Path(__file__).resolve().parents[1] / "dags")
# Airflow 3.3 dropped DagBag(include_examples=); use configuration instead.
os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
os.environ["AIRFLOW__CORE__UNIT_TEST_MODE"] = "True"


@pytest.fixture(scope="session")
def airflow_db() -> None:
    from airflow.utils import db

    db.initdb()
