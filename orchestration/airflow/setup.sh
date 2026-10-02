#!/usr/bin/env bash
# Keep Airflow's dependencies separate from the gridcase environment.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
uv venv --python 3.12 "$here/.venv"
uv pip install --python "$here/.venv/bin/python" \
  -c https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt \
  apache-airflow==3.3.2 pytest
