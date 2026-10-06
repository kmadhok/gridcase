# MarketCase M12 — risk spikes before building (loops 1–3)

Status: **superseded 2026-10-03** by `../../../ShelfCase/docs/PLAN.md` (retail edition for the Databricks Retail FDE role). Kept for reference; do not implement. Was: proposed 2026-10-03. This gates `docs/plans/2026-10-03_lakehouse_edition.md`: M12a starts only after loops 1 and 2 have a verdict for every P0 risk.

## Why

The M12 plan rests on assumptions that are cheap to test and expensive to discover late. A wrong trade-id formula breaks every parity check. A quota cap could kill the hourly cadence. If skew is mild, the Spark experiment needs synthetic data. Each risk gets the smallest experiment that can confirm or refute it, with the pass/fail line written before it runs.

## The loop

Every spike is one script, `experiments/risk_spikes/rN_<name>.py`, which prints a single JSON verdict:

```json
{"risk": "R1", "status": "confirmed | refuted | inconclusive", "numbers": {}, "evidence": "path or query", "iteration": 1}
```

- **Run.** `experiments/risk_spikes/run.sh` runs every spike whose inputs are present. It skips spikes that need Databricks when `DATABRICKS_TOKEN` is unset, rather than failing them. Verdicts are appended to `experiments/risk_spikes/verdicts.jsonl`.
- **Judge.** Each verdict is compared against the pass line below. Nobody gets to "eyeball it".
- **Tighten.** If a spike is inconclusive, change only what made it inconclusive (more data, a sharper query), then rerun. After 3 inconclusive iterations, stop and ask the user.
- **Act.** A confirmed risk applies its fallback to the M12 plan before any build starts. Every verdict, with its numbers, gets a DECISIONS entry tagged `REVIEW:`.
- **Read-only on the VPS.** Spikes read the collector's raw `.jsonl.gz` files only, never `data/gridcase.duckdb` or the dbt warehouse, because DuckDB locks a file to its writer. Nothing on the VPS is restarted or changed.

## Loop 1 — no credentials needed (laptop + raw files)

Setup:
- `brew install openjdk@17`.
- A separate `experiments/risk_spikes/.venv` with `pyspark` and `duckdb`, never added to gridcase's main dependencies. Pin PySpark to the Spark major version that Databricks serverless runs; check the release notes at setup.
- `rsync` one full closed UTC day of raw trade files (plus the hour files on either side) from the VPS into `experiments/risk_spikes/data/`. That directory is gitignored.

| ID | Risk (what would break) | Experiment | Pass line, written first | If confirmed |
|---|---|---|---|---|
| **R1** P0 | DuckDB and Spark format `size`/`price` doubles differently in `cast(... as string)`, so `trade_id = md5(tx\|asset\|side\|size\|price)` differs and parity fails everywhere | Compute `trade_id` in both engines over every row of the day. Join on (tx, asset, side, row position). Count mismatches, then list the distinct number values whose string forms differ. Add edge values: 1e-05, 1e-7, 123456789.5, 0.1+0.2, trailing zeros | 0 mismatches over the whole day **and** the edge values | Preferred: Spark reproduces DuckDB's exact string form with a small UDF tested against every mismatch found, so existing ids in the app store and dbt stay valid. Otherwise both sides hash `cast(cast(x as decimal(38,10)) as string)`; that changes every stored id and needs a migration plan the user approves |
| **R2** P0 | USD sums differ by more than the parity tolerance (relative 1e-9), because floating-point addition order varies across engines and partitions | Daily and hour-by-category sums of `usd` in both engines. Spark runs at 1, 8 and 200 shuffle partitions | Max relative difference < 1e-9 on every cell | Raise the tolerance to the measured maximum × 10 with a written reason, or sum in `decimal(38,10)` in both engines |
| **R3** P1 | Real data has too little skew to show anything in M12f's experiment | DuckDB: share of trades from the top 1% of wallets and the top 10 markets. Spark: hash-partition by wallet into 200 partitions and compare the largest partition's rows with the median | Largest partition ≥ 5× the median → skew is real | Use the scale generator with planted skew, labelled "synthetic" in every table and sentence |
| **R4** P0 | The collector writes to an hour's file after that hour closes, so landing files change after upload and Auto Loader silently misses rows | On the VPS, for every trade file, compare its mtime with the end of the hour in its path. Read-only `stat`, no file opens | Max lateness is known. The upload's "closed" margin is set to max lateness + 5 minutes, and no file is still growing an hour later | Upload hours only after margin + 1 hour, or switch Auto Loader to `cloudFiles.allowOverwrites` with an explicit `trade_id` MERGE, which is already idempotent |
| **R4b** P0 | The collector's files are concatenated gzip members (appends), and Spark reads only the first member, silently undercounting | Row count per file: Spark `read.json` versus DuckDB `read_json` versus `zcat \| wc -l` | All three agree for every file | Recompress each closed hour into a single gzip member during upload |
| **R10** P2 | The open-source Spark used in M12f behaves differently from serverless | Read the current serverless runtime release notes. Record the Spark version and the Photon/AQE defaults | Version recorded | Pin the experiment's Docker image to match |

## Loop 2 — needs the Databricks workspace and token

Run from the **VPS** (the machine that will do the real uploads), except R1b.

| ID | Risk | Experiment | Pass line | If confirmed |
|---|---|---|---|---|
| **R5** P0 | The VPS cannot reach the workspace, the PAT does not work on Free Edition, or bundles do not deploy to serverless | `databricks current-user me`. Upload one closed hour to a volume. `bundle deploy -t dev` a one-task hello job, run it, and read its output | All four succeed | Use the Databricks Python SDK over plain HTTPS. If the network is blocked, upload from the laptop instead and record the hybrid claim as weaker |
| **R6** P0 | A Free Edition feature this plan relies on is missing | One SQL script on the Starter warehouse: `CREATE CATALOG` (expected to fail; record it), `CREATE SCHEMA`, `CREATE VOLUME`, a `WITH METRICS` view, a call to the Statement Execution API, an MLflow `start_run`, and registering a dummy model in Unity Catalog | Each capability marked yes or no | Each "no" maps to an edit in M12, e.g. no metric views → gold views with fixed grains plus `MEASURE`-free SQL |
| **R8** P1 | Metric views cannot express two-step KPIs (per item, then across items) or share KPIs | Write metric views by hand for one simple, one share and one two-step KPI. Compare values with DuckDB for the same day | Simple and share KPIs match. Two-step: matches, or marked unsupported | Unsupported two-step KPIs become gold tables (the M12c fallback) |
| **R1b** P0 | Serverless Spark (Photon, ANSI on) formats doubles differently from open-source Spark | Rerun R1's edge values and one hour of real rows in a serverless notebook | 0 mismatches with DuckDB | Same fallback as R1 |

## Loop 3 — 48-hour soak in `dev`

| ID | Risk | Experiment | Pass line | If confirmed |
|---|---|---|---|---|
| **R7** P0 | The hourly job uses up the fair-use quota | Run a representative bronze→gold job over one day, 3 times, and record durations. Then schedule it hourly in `dev` for 48 hours | No compute shutdown in 48 hours. Record job minutes per day | Every 3 hours, or switch gold to incremental `replaceWhere` and rerun the soak |
| **R9** P2 | The new Airflow tasks push `gridcase-airflow` past its memory cap | Peak memory of `gridcase lakehouse upload` for one hour of files, measured with `/usr/bin/time -v` | Peak < 300 MB, with unit headroom left | Stream uploads file by file; never load a day into memory |

## Order and gates

1. **Loop 1:** start today. It needs Java, a separate venv and one day of raw files; no account.
2. **Loop 2:** as soon as credentials exist. It takes about an hour.
3. **Loop 3:** runs in the background while M12a is built.

Gates:
- M12a needs R4, R4b, R5 and R6 resolved.
- M12b needs R1, R1b and R2.
- M12c needs R8.
- M12f needs R3 and R10.
- Moving to hourly prod needs R7.

## Outside this loop

M5's license (for M13) is a reading task for the user on Kaggle, not an experiment. It is recorded here so it is not forgotten.
