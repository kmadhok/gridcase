# MarketCase M10 — Airflow orchestration and a Hex-ready mart

Status: approved by the user 2026-10-02 ("can't we just implement airflow and hex into the marketcase project?").

## Why

The Polymarket Senior Analytics Engineer posting asks for "dbt or equivalent, Airflow or other orchestration" and "BI tooling such as Hex". MarketCase schedules dbt with a systemd timer and has no Hex surface. M10 makes both real: Airflow becomes the scheduler of the warehouse build, and the marts are published where Hex can query them.

## Decisions

1. **Airflow replaces the `gridcase-dbt` timer**; it does not run beside it. One scheduler, so the claim is true. The timer and its service are removed (install.sh disables them, like the old ingest timer).
2. **Airflow lives in its own uv project, `orchestration/airflow/`**, pinned to apache-airflow 3.3.2 with the official constraints for Python 3.12. Its dependency tree never mixes with gridcase's. Tasks call the gridcase CLI (`uv run gridcase …` in the repo root), so the DAG imports nothing from gridcase.
3. **`airflow standalone` under a user unit `gridcase-airflow`**, `AIRFLOW_HOME=~/.local/share/marketcase-airflow`, SQLite metadata, examples off, API/UI bound to the Tailscale address 100.115.94.44:8711 only (never public, not through the tunnel). `MemoryMax=1500M` so it cannot starve T3, Hermes or the MarketCase server (the VPS has ~2.8 GB available and no swap). `REVIEW:` standalone is Airflow's single-machine mode; a team setup would run the scheduler, API server and DAG processor separately on Postgres.
4. **DAG `marketcase_warehouse`**, hourly at :20 (the old timer's slot), `catchup=False`, `max_active_runs=1`, 2 retries 5 minutes apart, 30-minute task timeout:
   `check_sources` → `dbt_build` → `dbt_docs` → `publish_marts`, in a line: DuckDB lets only one process open a file while another writes it, so publish must not overlap dbt's docs run on the warehouse.
   - `check_sources`: `gridcase warehouse check-sources` fails if the newest raw trade file or `data/exports/markets.parquet` is older than 15 minutes, so a stalled collector or server stops the build instead of producing a quietly stale mart. No backfill: the public feed cannot be replayed (see CLAUDE.md), so catchup stays off.
5. **Hex reads MotherDuck.** Hex connects to MotherDuck natively; it cannot reach a DuckDB file on the VPS. `publish_marts` (`gridcase warehouse publish`) replaces three small tables in the MotherDuck database `marketcase`: `metrics_daily`, `metrics_hourly_by_category` (new) and `dim_markets`. Trades are not published (millions of rows a day; Hex questions here are about metrics). Without `MOTHERDUCK_TOKEN` in the environment the task logs "skipped" and succeeds, so the DAG works before the user has an account.
6. **New mart `metrics_hourly_by_category`**: one row per UTC hour and category with volume_usd, trades, active_traders, whale_share, buy_share. Same formulas as `metrics_daily`; a singular dbt test checks that summing it over a day gives `metrics_daily`'s volume_usd and trades. Not a new KPI, the existing KPIs by an existing dimension. Reason: with three days of record a daily chart has three points; hourly by category is what a Hex app can usefully show today.
7. **The Hex app is built by the user in Hex** (Community plan, free) from `docs/hex.md`: connection steps and the SQL for three cells. Building it by hand is the point: it is the experience the posting asks about.

## Exit test (written first, run red)

- `tests/exit/test_m10.py` (main env): check-sources passes on fresh files, fails on a stale raw file, fails on a stale or missing markets export; publish copies the three tables to a target (a local DuckDB file stands in for MotherDuck via `MARKETCASE_PUBLISH_TARGET`) with identical rows; publish without a token and the default target is a no-op that exits 0.
- `orchestration/airflow/tests/test_dag.py` (Airflow env): DagBag has no import errors; task ids and the four-task chain as above; schedule, catchup, max_active_runs, retries.
- `tests/unit/test_deploy_units.py`: the Airflow unit binds the Tailscale address, has MemoryMax, sets examples off; install.sh no longer installs or enables `gridcase-dbt.timer`.
- dbt: unique (hour, category), not-null keys, accepted categories, shares between 0 and 1, and the day-sum test.
