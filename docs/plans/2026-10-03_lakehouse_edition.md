# MarketCase M12 — lakehouse edition (Databricks, Spark, MLflow, CI/CD)

Status: **superseded 2026-10-03** by `../../../ShelfCase/docs/PLAN.md` (retail edition for the Databricks Retail FDE role). Kept for reference; do not implement. Was: proposed 2026-10-03, awaiting the user's go-ahead on the decisions at the end. Gated by `docs/plans/2026-10-03_lakehouse_risk_spikes.md`: no phase starts until its risks have verdicts.

## Why

The Databricks Sr. Forward Deployed Engineer (Retail) posting asks for deep Apache Spark including runtime internals, production CI/CD, working MLOps, and Databricks itself. The user's resume backs Spark only with a 2023 coursework project and backs none of the others. M12 makes them real on the data MarketCase already collects. Each sub-phase must leave two things: a measured entry in `docs/DECISIONS.md`, and a short "you should be able to explain" list the user learns before any of it reaches a resume. In an interview, a claim the user cannot defend at a whiteboard costs more than leaving it out.

## Where things run

- **VPS (unchanged):** collector, `gridcase-server` (DuckDB, agent, public UI) and the Airflow `marketcase_warehouse` DAG. The public site keeps reading DuckDB and never depends on Databricks.
- **VPS (new):** a second Airflow DAG, `marketcase_lakehouse`, that uploads closed hours of raw files, starts the Databricks job, and checks parity. Its tasks are HTTP calls, so they add little memory under `gridcase-airflow`'s `MemoryMax`. Peak memory is measured at deploy.
- **Databricks Free Edition:** one scheduled job, plus metric views and MLflow experiments. Nothing there needs to stay up.
- **Laptop:** the Spark internals experiment (M12f), run once in Docker.
- **GitHub Actions:** tests on every push, and bundle deploys to Databricks.

## Free Edition limits this plan is built around (checked 2026-10-03)

Sources: docs.databricks.com free-edition-limitations, compute/serverless/limitations, spark/conf.

- **Compute:** serverless only, with one 2X-Small SQL warehouse and at most 5 concurrent job tasks. A fair-use quota can shut compute off for the day or the month.
- **Languages:** no Scala or R.
- **Streaming:** only `Trigger.AvailableNow` (or the deprecated `Once`). `ProcessingTime` and `Continuous` raise `INFINITE_STREAMING_TRIGGER_NOT_SUPPORTED`.
- **Tuning surface:** no Spark UI (query profile only), no RDD API, no `cache()`/`persist()`. Only six Spark properties can be set, and none is `spark.sql.adaptive.*`. This is why the internals experiment runs on open-source Spark (M12f).
- **Network and apps:** outbound internet is limited to approved domains, so Databricks cannot call Polymarket and the VPS pushes files instead. Apps stop after 24 hours.
- **Other:** non-commercial use only, one workspace, no account console. Personal access tokens work with the CLI and bundles. Community reports say OAuth can fail on Free Edition.

## Prerequisites (user, before M12a)

1. Create a Databricks Free Edition workspace.
2. Generate a personal access token with an expiry. `REVIEW:` rotate it before it expires; M12d's parity task fails loudly on a 401.
3. Put `DATABRICKS_HOST`, `DATABRICKS_TOKEN` and `DATABRICKS_WAREHOUSE_ID` (the Serverless Starter Warehouse's ID, used by M12c–d's SQL calls) in the laptop `.env`, and copy them to the VPS `.env` with `scp` on the user's say-so (existing rule; the values never pass through Claude). Add both as GitHub repository secrets for M12e.

## M12a — landing zone and a live smoke test

1. **Bundle `lakehouse/databricks.yml`** (Declarative Automation Bundles, formerly Asset Bundles), with targets `dev` and `prod`. It defines:
   - three schemas in the workspace's default catalog: `mc_bronze`, `mc_silver`, `mc_gold`;
   - a volume `mc_bronze.landing`;
   - the job from M12b, on serverless compute.

   `REVIEW:` three schemas rather than three catalogs, because catalog creation on Free Edition is unverified.
2. **`gridcase lakehouse upload`** copies collector files from `data/raw/polymarket/trades/*/*/*/*.jsonl.gz` into `/Volumes/<catalog>/mc_bronze/landing/trades/` with the same relative path. It uploads only **closed hours**: the file's hour is before the current UTC hour and its mtime is more than 10 minutes old.
   - Reason: the collector appends to the current hour's file, and Auto Loader does not reprocess a file it has already ingested (`cloudFiles.allowOverwrites` is off). Landing files must therefore be immutable.
   - Cost: up to about 1–2 hours of lag.
   - A manifest at `data/lakehouse_manifest.json` records path, size and mtime, so re-runs upload nothing new. `markets.parquet` is uploaded once an hour as `markets/markets_YYYYMMDDHH.parquet`.
3. **No token, no-op.** Without `DATABRICKS_TOKEN`, upload logs "skipped" and exits 0, the same pattern as the MotherDuck publish.
4. **Live smoke test (manual, once):** `scripts/lakehouse_smoke.sh` deploys the bundle to `dev`, uploads one closed hour, runs the job, and prints the bronze row count. Its output goes in DECISIONS. This is the first real proof that the VPS can reach the workspace.

**Exit test** `tests/exit/test_m12a.py`, using a fake Files client:
- the open hour and fresh files are never selected;
- a second run uploads nothing;
- a changed closed file is reported as an error rather than overwritten;
- with no token, upload exits 0.

**You should be able to explain:** volumes versus tables in Unity Catalog; why landing files must be immutable for Auto Loader; what the manifest protects against.

## M12b — bronze, silver, gold as a plain Spark job

The job runs on serverless with three tasks: `bronze` → `silver` → `gold`, then a `quality` task. The code lives in `lakehouse/src/marketcase_lakehouse/`. Transforms are pure functions from DataFrame to DataFrame, so local PySpark can test them.

1. **Bronze:**
   - Auto Loader (`cloudFiles.format=json`) with schema hints for the trade fields and the rescued-data column kept.
   - Adds `_metadata.file_path` as `source_file`, plus an `_ingested_at` column.
   - Runs with `Trigger.AvailableNow`, checkpointed in the volume. An append-only Delta table.
2. **Silver:**
   - Built from a `foreachBatch` stream over bronze. Each batch is first deduplicated by `trade_id` with a window, then MERGEd into `mc_silver.trades`: insert when there is no match; on a match, update when the incoming copy was collected earlier. The earliest copy wins, as in `stg_polymarket__trades`.
   - The `trade_id` formula must match dbt exactly: md5 of `transactionHash|asset|side|size|price`.
   - `REVIEW:` DuckDB and Spark format doubles differently when cast to strings (e.g. `1e-05` vs `1.0E-5`). M12b must prove the two engines produce the same ids on every distinct `size`/`price` in a real day of data. If they differ, both sides switch to a fixed decimal format, and that change goes through dbt's tests too.
   - Markets: silver keeps the newest snapshot per `condition_id`.
3. **Gold:** `fct_trades`, `dim_markets`, `metrics_daily` and `metrics_hourly_by_category`, with the same columns and formulas as the dbt marts. Gold is rebuilt in full from silver on each run.
   - `REVIEW:` the full rebuild is a deliberate first choice. Each run's duration is recorded (M12d). Gold switches to incremental `replaceWhere` on touched days only if a run takes more than 10 minutes or the quota shows strain, and the measurement goes in DECISIONS.
4. **Quality:** a final task asserts what dbt tests today and fails the job on any breach:
   - unique keys and non-null keys;
   - `usd = size × price`;
   - shares between 0 and 1;
   - each day's hourly-by-category rows sum to that day's `metrics_daily`.

**Exit test** `tests/exit/test_m12b.py`, using local PySpark in the dev group (needs Java 17 on the laptop and in CI):
- run the transforms over the existing fixtures (`tests/fixtures`, `tests/polymarket_synth.py`) and compare every gold row with dbt-duckdb's output on the same fixtures: exact for counts, relative 1e-9 for USD sums;
- duplicate copies across batches keep the earliest;
- the quality checks fail on a planted bad row.

**You should be able to explain:**
- what a checkpoint stores, and why `AvailableNow` is "incremental batch", not always-on streaming;
- how MERGE makes reprocessing idempotent;
- the Delta transaction log and time travel;
- why USD sums differ in the last bits across engines (floating-point addition order).

## M12c — metric views generated from `kpis.yaml`

1. **`gridcase lakehouse metric-views`** compiles `ontology/kpis.yaml` into Unity Catalog metric view definitions over `mc_gold.fct_trades`, creating dimensions and measures. They are applied with `CREATE OR REPLACE VIEW … WITH METRICS LANGUAGE YAML` through the SQL warehouse. The YAML stays the one definition; Databricks receives a compiled copy, as DuckDB does (hard rule 1 extended).
2. **Two-step KPIs** (per item, then across items) are emitted only if the metric view language can express them. Otherwise they are listed as unsupported, with the reason. No KPI is silently dropped. `REVIEW:` the user decides whether an unsupported KPI should become a gold table instead.

**Exit test** `tests/exit/test_m12c.py`:
- golden-file YAML for one simple, one share and one month-to-date KPI;
- every KPI in `kpis.yaml` is either emitted or listed as unsupported.

**You should be able to explain:** what a metric view adds over a plain view (measures evaluated at query grain via `MEASURE()`); how this maps to the semantic layer you built at Walmart.

## M12d — parity check and orchestration

1. **`gridcase lakehouse parity`** queries the metric views through the Statement Execution API. It compares the last 24 closed hours by category and the last complete UTC day with the DuckDB warehouse marts.
   - Counts must match exactly; USD values within a relative 1e-9.
   - Hours not yet landed are excluded and listed rather than counted as failures.
   - It writes `data/lakehouse_parity.json`, including the Databricks job's run duration from the Jobs API, and exits non-zero on any mismatch.
2. **New DAG `marketcase_lakehouse`**, hourly at :40, after the warehouse DAG's :20 run:
   - tasks `upload` → `run_job` → `parity`, where `run_job` starts the bundle's job and waits for it;
   - `catchup=False`, `max_active_runs=1`, 1 retry, a 45-minute timeout;
   - as in M10, tasks call the gridcase CLI and the DAG imports nothing from gridcase.
   - `REVIEW:` a separate DAG, so a Databricks quota shutdown never blocks the MotherDuck publish or Hex.
   - `REVIEW:` the Databricks Airflow provider's `DatabricksRunNowOperator` is the more conventional choice. It is not used, to keep the constrained Airflow environment's dependencies unchanged.
3. **Quota watch:** after 3 days of runs, record the total job minutes per day in DECISIONS. If compute shuts off even once, move the cadence to every 3 hours.

**Exit test** `tests/exit/test_m12d.py`, plus `orchestration/airflow/tests/test_lakehouse_dag.py`:
- parity passes on equal data, fails on a planted mismatch, and lists late hours separately;
- the DagBag loads, and the task chain, schedule and retries are as above;
- the warehouse DAG is unchanged.

**You should be able to explain:** why two warehouses must agree, and what a parity failure would tell you; how a customer-side Airflow would trigger Databricks jobs (the FDE situation).

## M12e — CI/CD

1. **`.github/workflows/ci.yml`** runs on every push and PR:
   - `uv sync`, `ruff`, `mypy --strict`, and pytest (unit and exit tests, with Java 17 for the PySpark tests);
   - the web app's tests and its TypeScript build;
   - the Airflow DagBag tests in their own env.

   Live tests are skipped when there is no token.
2. **`.github/workflows/deploy-lakehouse.yml`**, on pushes to `main` that touch `lakehouse/` or `ontology/kpis.yaml`, runs `databricks bundle validate`, then `databricks bundle deploy -t prod`, then applies the metric views. It uses the official `databricks/setup-cli` action and the repository secrets.
   - `REVIEW:` the VPS still deploys by hand with `deploy/install.sh`. CI does not SSH into the VPS.

**Exit:** a green CI run on GitHub and one successful deploy run, both linked in DECISIONS.

**You should be able to explain:** what the bundle validates and deploys; why dev and prod are separate targets; how a token secret reaches the deploy job without being logged.

## M12f — Spark internals experiment (laptop, open-source Spark)

Free Edition hides the Spark UI and the AQE settings, so this phase runs where both are visible.

1. **Setup:**
   - `experiments/spark_skew/docker-compose.yml`: a Spark standalone master, 2 workers and a History Server, with event logs in a local directory.
   - `REVIEW:` pin the Spark version to the major version Databricks serverless runs; check the runtime release notes at implementation time.
2. **Data:**
   - First, export silver-equivalent trades to Parquet from the VPS DuckDB.
   - Then measure skew: the share of trades held by the top 1% of wallets and by the top 10 markets.
   - If natural skew is too mild to matter, use the existing scale generator with a planted skew, labelled as synthetic everywhere it appears.
3. **Experiments,** each run 3 times, reporting the median:
   1. Join trades to per-wallet aggregates, run three ways: AQE off; AQE on, with skew-join handling; manual salting.
   2. Shuffle partitions at the default 200, versus AQE coalescing.
   3. A broadcast join versus a sort-merge join to `dim_markets`.
4. **Measured and parsed from the event logs by a script, not read off the screen:** wall time, max versus median task time in the skewed stage, shuffle read bytes and spill bytes. Every variant must produce identical output rows.
5. **Output:** `docs/spark_experiments.md` holds a results table plus one paragraph per mechanism, and DECISIONS gets an entry.

**Exit test** `tests/exit/test_m12f.py`: the event-log parser returns the expected numbers on a small checked-in log, and the variants' outputs are equal on a fixture.

**You should be able to explain:**
- stages, tasks and shuffles; how a sort-merge join works;
- what skew does to one task, and how AQE splits skewed partitions;
- salting, and why it costs more than it looks;
- broadcast thresholds; spill.

This is the answer to "deep Spark, runtime internals".

## M12g — MLflow

1. **Now:**
   - The lakehouse DAG logs one MLflow run per job run in a Databricks experiment. Each run records row counts, quality results, parity results and job duration.
   - It also logs M11's case traces (tokens, cost, latency, sentences kept versus dropped by the verifier) as an eval history, in batches from the DAG and never from the live request path.
   - `REVIEW:` which M11 trace fields count as eval metrics.
2. **Gated on at least 14 days of record (no earlier than 2026-10-13):**
   - an hourly volume forecast per category, trained in a Databricks job on gold;
   - evaluated on the last 3 days held out (MAPE and interval coverage) against today's rolling-average range as the baseline;
   - registered in Unity Catalog with a `champion` alias only if it beats the baseline.

   Free Edition's serving limits mean scoring runs in a job, not an endpoint. The watch keeps the current detector: changing what raises alarms is a decision under hard rule 5.

**Exit test** `tests/exit/test_m12g.py`: run logging works against a local MLflow file store; the alias is not set when the model loses to the baseline.

**You should be able to explain:** experiments, runs and the model registry; why a model must beat a simple baseline before it ships; offline evaluation versus live monitoring.

## After M12: retail edition (separate plan, M13)

The same engine runs on a public retail dataset, so the claim "I built a reusable accelerator and redeployed it to a new domain" is true. The candidate dataset is M5 Forecasting (Walmart hierarchical sales), with two gates first:
1. **License:** the Kaggle competition rules must allow public, non-competition use. This could not be confirmed on 2026-10-03.
2. **Hard rule 5:** a new data source needs a DECISIONS line and a stop for the user's review.

Instacart or the UCI Online Retail dataset are fallbacks if M5's license blocks it.

## Order

M12a → M12b → M12c → M12d → M12e. M12f can run on the laptop at any point and is the most valuable for interviews, so start it early. M12g part 2 waits for 14 days of record.

## What becomes resume-safe, and when

Nothing goes into `Resume Achievements Master.md` (interview-prep repo, entry G5) until the phase is deployed or run and its DECISIONS entry holds the measurements. Then:

| Posting asks for | Phase | Claim it supports |
|---|---|---|
| Databricks, Delta, Unity Catalog | M12a–c | A medallion lakehouse fed hourly from a live collector, with metric views compiled from the governed KPI layer |
| Spark and runtime internals | M12b, M12f | Measured skew, AQE and join-strategy experiments, with numbers |
| CI/CD for production | M12e | Tests on every push; bundle deploys to Databricks |
| MLOps | M12g | Run tracking, eval history, and a registered model only if it beats the baseline |
| Production end-to-end architecture | M12d | Hybrid: Airflow on a self-hosted server drives two warehouses, with a parity test |

## Decisions for the user

1. **A plain Spark job** (recommended: checkpoints, triggers and MERGE stay visible and defensible) **or Lakeflow Declarative Pipelines** (more Databricks-native, adds data-quality "expectations", but hides those mechanics)?
2. **A separate `marketcase_lakehouse` DAG** (recommended) or more tasks on the warehouse DAG?
3. **Gridcase CLI tasks** (recommended: no new Airflow dependencies) or the Databricks Airflow provider?
4. **Upload closed hours only**, accepting about 1–2 hours of lag in the lakehouse (recommended)?
5. **Is the GitHub deploy workflow allowed to hold a Databricks token** as a repository secret?
