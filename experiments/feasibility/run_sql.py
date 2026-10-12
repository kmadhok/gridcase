# ruff: noqa: E501
"""Run a .sql file statement by statement and print each result (used for vps_inventory.sql).

nice -n 19 ionice -c3 uv run python experiments/feasibility/run_sql.py experiments/feasibility/vps_inventory.sql
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb


def main(path: str) -> None:
    con = duckdb.connect()
    for chunk in Path(path).read_text().split(";"):
        lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("--")]
        sql = "\n".join(lines).strip()
        if not sql:
            continue
        title = next((ln.strip() for ln in chunk.splitlines() if ln.strip().startswith("-- ")), "")
        res = con.execute(sql)
        if res.description and not sql.upper().startswith(("SET", "CREATE")):
            print(f"\n{title}\n" + " | ".join(d[0] for d in res.description))
            for row in res.fetchall():
                print(" | ".join(str(v) for v in row))


if __name__ == "__main__":
    main(sys.argv[1])
