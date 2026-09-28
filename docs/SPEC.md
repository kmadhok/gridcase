# GridCase — Claude Code Handoff Spec

Sep 28, 2026 · @The Programmer

## How to use this doc

Claude Code (Opus 5.5) can build roughly 80% of GridCase unattended; the other 20% is judgment calls listed in the intervention checklist at the end. Each phase below ends with an **exit test** that a Ralph-style loop can run until it passes, so hand Claude Code one phase at a time and let it iterate.

1. Create an empty repo `gridcase`, paste the CLAUDE.md section into `CLAUDE.md`, and paste this whole doc into `docs/SPEC.md`.
2. Get an EIA API key at eia.gov/opendata (free, instant) and put it in `.env` as `EIA_API_KEY`. Claude Code must never see or write the key value.
3. Paste the kickoff prompt (last section) into Claude Code with plan mode on. Approve the plan for Phase 0 only.
4. Review at each phase boundary: read the exit-test output, not the code. Move to the next phase only when the test is green and you agree with the decisions flagged for you.
5. Use a git worktree per phase if you want to run Phase 5 (benchmark) in parallel with Phase 4 (planner); Phases 0–3 are strictly sequential.

What Claude Code does well here: ingestion, schema, deterministic tools, tests, the MCP server, the UI, the fault injector, scoring code. What it does badly without you: choosing hypothesis templates, labeling ground truth, deciding what counts as "right," and judging whether a benchmark score is honest.

## Charter

GridCase is a governed investigation engine for plan-vs-actual metrics, proven on the US electric grid and scored on a public replay benchmark. When a metric misses its plan, it detects the miss against an expected-value model, plans an investigation as a typed DAG of deterministic tools, tests competing causal hypotheses, writes a conclusion where every sentence cites a receipt, takes one governed action, and re-checks itself when lagged data lands.

The deliverable is the benchmark, not the agent. The agent is the exhibit; the published root-cause accuracy, false-alarm rate, calibration and receipt coverage are the credential.

**In scope for v1:** ERCOT, CAISO, PJM and the US48 aggregate. About 15 metrics. Winter Storm Uri (Feb 2021) plus 4 other documented events as replay cases. \~200 fault-injected cases. One action (publish case + schedule re-check). A stateless MCP server. A web UI with a US map and case viewer.

**Explicitly out of scope:** a chat interface as the primary UX, LLM-written SQL against raw tables, a general action runtime, a policy engine, lineage, more than 4 regions, real-time (sub-hourly) data, any paid data source.

**Non-negotiable design rules:**

- The LLM never writes SQL. It selects metrics, entities and hypotheses; a deterministic compiler produces SQL.
- The explainer may only emit sentences that cite a receipt from the evidence ledger. Uncited sentences are dropped or labeled `inferred`.
- Every tool call takes `as_of` and reads only data with `observed_at <= as_of`. Replay must be leak-free.
- The plan validator must reject any plan that skips the data-quality hypothesis or tests fewer than two hypotheses.
- Fault injection is a first-class module, not a test fixture.

## CLAUDE.md (copy verbatim to repo root)

```markdown
# GridCase

Governed investigation engine for plan-vs-actual metrics, proven on US grid data, scored on a public replay benchmark. Full spec: docs/SPEC.md. Read it before any non-trivial change.

## Stack
- Python 3.12, uv for env and deps, ruff + mypy --strict, pytest + hypothesis
- DuckDB for the bitemporal store (single file, data/gridcase.duckdb); Parquet snapshots in data/raw/
- Pydantic v2 for every plan, receipt, hypothesis, and ontology object
- Anthropic SDK for the planner and explainer only; model name in config, never hardcoded
- FastAPI for the MCP server (MCP 2026-07-28 spec, stateless, Tasks extension for re-checks)
- Web UI: single-page app in web/, Vite + React + MapLibre; no backend beyond the FastAPI service
- Secrets only via .env, loaded with python-dotenv; never print, log, or commit them

## Hard rules
1. The LLM never writes SQL. Metrics compile to SQL via gridcase/semantic/compiler.py only.
2. Every tool in gridcase/tools/ is a pure function of (inputs, as_of) and returns a Receipt. No tool reads data with observed_at > as_of.
3. The explainer emits only sentences that reference receipt ids. Enforce in gridcase/explain/verifier.py; uncited sentences are dropped.
4. Plans are gridcase/planner/schema.py::InvestigationPlan. The validator rejects plans that omit hypothesis H0_DATA_ARTIFACT or test < 2 hypotheses.
5. No new data source, hypothesis template, or action type without a line in docs/DECISIONS.md and a stop for human review.
6. Do not create a chat UI. The case viewer is the UI.

## Workflow
- Work one phase at a time from docs/SPEC.md. Each phase has an exit test in tests/exit/. Loop until it passes.
- Before coding a phase: write the exit test first, run it red, then implement.
- After each phase: append a dated entry to docs/DECISIONS.md listing every judgment call you made, then STOP and ask for review.
- Commit at every green test with a conventional-commit message. Never force-push.
- Keep responses short. Report: what passed, what failed, what you decided, what you need from me.

## Commands
- uv run pytest tests/exit/test_phase_N.py -x
- uv run gridcase ingest --source eia --start 2021-02-13 --end 2021-02-21
- uv run gridcase replay --case uri_2021 --as-of 2021-02-15T12:00Z
- uv run gridcase bench --suite all
- uv run gridcase serve

## Data caveats you must respect
- EIA demand during load-shed events is SERVED load, not true demand. Never conclude "demand fell" during an EEA event without flagging this.
- EIA revises history. Store observed_at on every row; never overwrite, always append.
- Fuel-type categories changed in 2024 Q3 and ERCOT split out batteries in Oct 2024. Map old and new categories in the ontology, not in queries.
- gridstatus scrapers break. Pin the version, cache raw responses, and fail soft.
```

## Architecture

&#91;embedded content: GridCase architecture · 5 layers, 2 LLM calls\]

Data flows top to bottom; case memory feeds back into the planner's priors, so the loop closes across cases, not just within one. Repo layout mirrors the bands: `gridcase/ingest`, `gridcase/store`, `gridcase/semantic`, `ontology/`, `gridcase/detect`, `gridcase/planner`, `gridcase/tools`, `gridcase/ledger`, `gridcase/explain`, `gridcase/actions`, `gridcase/memory`, `gridcase/bench`, `gridcase/server`, `web/`, `tests/exit/`, `docs/`.

## Data sources

All three sources are free; EIA is public domain, the others need attribution. Every source is polled hourly by a cron job and appended to the bitemporal store with an `observed_at` stamp.

| Source | Endpoint / library | Series | Cadence and lag | Limits | License |
| --- | --- | --- | --- | --- | --- |
| EIA API v2 | `https://api.eia.gov/v2/electricity/rto/region-data/data/` | Hourly demand (D), day-ahead forecast (DF), net generation (NG), total interchange (TI) per BA | Demand hourly, \~1 h lag; forecast \~10:00 ET daily; NG and TI 1-day lag | 5,000 rows/request; sustain < 9,000 req/h and < 5 req/s | Public domain; credit "U.S. EIA" |
| EIA API v2 | `.../electricity/rto/fuel-type-data/data/` | Net generation by fuel type per BA | 1-day lag; history from 2018-07-01 | same | same |
| EIA API v2 | `.../electricity/rto/interchange-data/data/` | BA-to-BA interchange | 2-day lag | same | same |
| EIA API v2 | `.../electricity/rto/region-sub-ba-data/data/` | Subregion demand (ERCOT, CAISO, PJM zones) | 1–2 day lag; from 2018-07-01 | same | same |
| gridstatus (pip) | `gridstatus.Ercot()`, `gridstatus.CAISO()`, `gridstatus.PJM()` | Real-time and day-ahead LMP at hubs, load forecast, fuel mix (5-min), transmission outages where offered | Near real-time; scraped from ISO sites | Unofficial scrapers; pin version, cache raw | ISO public data; attribute |
| gridstatus.io hosted API | `https://api.gridstatus.io` with client `gridstatusio` | Same series, curated | Backfill only | Free plan 500,000 rows/month | Attribute |
| Open-Meteo | `https://archive-api.open-meteo.com/v1/archive` and `https://api.open-meteo.com/v1/forecast` | Hourly temperature, wind, cloud cover at BA load centers | Hourly; archive back decades | Non-commercial free tier, confirm current limits | CC BY 4.0 |

**Load-center coordinates for weather (initial set, refine later):** ERCOT → Houston, Dallas, Austin, San Antonio; CAISO → Los Angeles, San Francisco, Sacramento, San Diego; PJM → Chicago, Philadelphia, Washington DC, Pittsburgh; US48 → population-weighted mean of the above plus New York and Atlanta.

**Known data hazards** (each becomes a hypothesis template, not a bug to hide): \~2% missing demand hours historically; occasional negative or outlier values; interchange that violates conservation of energy; hourly generation that undercounts monthly totals; silent historical revisions.

## Phase 0 — Ground-truth gate (1 session)

Goal: prove Winter Storm Uri is recoverable hour-by-hour from the EIA API before writing any agent code. If this fails, stop and reconsider the dataset.

Build `scripts/phase0_uri_check.py` that pulls ERCOT (respondent `ERCO`) D, DF, NG, TI and fuel-type series for 2021-02-13 through 2021-02-21 and writes `data/raw/uri_2021/*.parquet` plus `docs/phase0_report.md`.

**Exit test `tests/exit/test_phase_0.py`:**

- [ ] 216 hourly rows (9 days) exist for each of D, DF, NG, TI; missing hours per series ≤ 4, and every gap is listed in the report.
- [ ] Peak gap `DF - NG` on 2021-02-15 is ≥ 25,000 MW (EIA reported at least 30,000 MW; allow for later revisions).
- [ ] Fuel-type data shows natural gas as the largest absolute drop between 2021-02-14 12:00 CT and 2021-02-15 12:00 CT.
- [ ] TI stays within ±1,500 MW for the whole window (ERCOT's limited ties).
- [ ] The report states, in one paragraph, whether "demand" in this window is served load and cites the EIA note on rotating outages.

**Human decision at the gate:** read the report and confirm you accept this as ground truth for the Uri case. Also pick the 4 other replay events; candidates the research surfaced are the June 2023 Texas heat dome, the Aug 2020 CAISO rolling blackouts, the Dec 2022 Winter Storm Elliott (PJM), and the April 8, 2024 solar eclipse (CAISO/ERCOT).

## Phase 1 — Bitemporal data layer (2 sessions)

Goal: every fact in the store answers "what did we know at time T?" so replay is leak-free and revisions become evidence.

**Tables (DuckDB):**

- `observations(series_id, entity_id, valid_time, value, observed_at, source, raw_hash)` — append-only; a revision is a new row with a later `observed_at`.
- `series(series_id, name, unit, source, lag_hours, history_start)`
- `entities(entity_id, kind, name, parent_id, tz, lat, lon)` — kinds: `ba`, `subregion`, `fuel`, `interchange_path`, `weather_zone`, `hub`.
- `ingest_runs(run_id, source, started_at, rows_written, status, error)`

**Views:** `as_of(series, entity, t)` returns the latest row per `valid_time` with `observed_at <= t`. All tools query through this view only.

**Ingestion:** `gridcase ingest --source {eia,gridstatus,openmeteo}` with idempotent backfill from 2018-07-01 for the four regions and an hourly cron mode. Raw API responses cached to `data/raw/{source}/{date}.json` before parsing.

**Exit test `tests/exit/test_phase_1.py`:**

- [ ] Backfill 2018-07-01 to today for ERCOT, CAISO, PJM, US48 completes; row counts per series match expected hours within 3%.
- [ ] Property test (hypothesis): for random `(series, entity, t1 < t2)`, `as_of(t1)` never contains a row with `observed_at > t1`, and `as_of(t2)` ⊇ the `valid_time` set of `as_of(t1)`.
- [ ] Re-running ingestion for an already-loaded day writes zero duplicate `(series, entity, valid_time, observed_at)` tuples.
- [ ] Injecting a synthetic revision (change one value, later `observed_at`) is visible in `as_of(later)` and invisible in `as_of(earlier)`.
- [ ] Ingestion of a day where EIA returns a 429 fails soft, logs, and retries with backoff without corrupting the run.

## Phase 2 — Semantic layer, ontology, hypothesis templates (2 sessions)

Goal: a governed metric layer the compiler can execute, plus an ontology that encodes causal mechanisms, not just definitions. This is the section where your existing compiler experience transfers directly.

**Semantic layer (`ontology/metrics.yaml`, OSI/Apache Ossie-compatible):** \~15 metrics with `authority: governed`. Minimum set: `demand`, `demand_forecast`, `forecast_error_mw`, `forecast_error_pct`, `net_generation`, `generation_by_fuel`, `net_interchange`, `balance_residual` (= D − NG − TI), `temp_load_center`, `hdd`, `cdd`, `lmp_rt_hub`, `lmp_da_hub`, `wind_capacity_factor_proxy`, `solar_capacity_factor_proxy`. Dimensions: `ba`, `subregion`, `fuel`, `hour_of_week`, `season`. The compiler in `gridcase/semantic/compiler.py` turns `MetricQuery(metric, entity, grain, window, as_of)` into SQL over the `as_of` view. Port the pattern from your Gen-2 work; do not let Claude Code improvise a new compiler design.

**Ontology (`ontology/entities.yaml`, `ontology/relations.yaml`):** entities and typed relations (`ba has_subregion`, `ba interchanges_with ba`, `ba served_by weather_zone`, `fuel reported_as fuel_category(version)`). Fuel-category versioning lives here so the 2024 schema change is handled once.

**Hypothesis templates (`ontology/hypotheses.yaml`)** — the differentiator. Each template is a Pydantic `HypothesisTemplate` with: `id`, `mechanism` (one sentence), `preconditions` (which metric anomaly triggers it), `tests` (ordered tool calls with expected signatures), `falsifier` (a boolean rule over tool outputs), `prior` (per BA and season, updated by case memory). Starting set of eight:

1. `H0_DATA_ARTIFACT` — mandatory first test. Signature: balance residual jump, missing hours, or revision > 5% within 48 h.
2. `H1_WEATHER_DEMAND` — forecast error correlates with temperature-forecast error in the same zone; peer BAs in the same weather front move together.
3. `H2_SUPPLY_SHORTFALL` — fuel bridge shows a single fuel contributing > 50% of the generation drop; LMP spikes.
4. `H3_IMPORT_CONSTRAINT` — interchange at or near historical max/min while price separation vs neighbor widens.
5. `H4_LOAD_SHED` — served load flat or falling while temperature-implied demand rises; EEA-style price cap behavior.
6. `H5_SUBREGION_CONCENTRATION` — one subregion explains > 60% of the miss.
7. `H6_RENEWABLE_FORECAST_MISS` — wind or solar proxy deviates from its trailing profile; residual demand error absorbs it.
8. `H7_CALENDAR` — holiday or daylight-saving boundary not reflected in the forecast baseline.

**Exit test `tests/exit/test_phase_2.py`:**

- [ ] Every metric in `metrics.yaml` compiles to SQL that executes against the store for all four regions without error.
- [ ] Property test: for any metric and any `as_of`, the compiled query returns the same result on two consecutive runs (determinism).
- [ ] `balance_residual` for ERCOT on a normal week (e.g. 2023-05-01) stays within ±3% of demand for ≥ 95% of hours.
- [ ] All eight hypothesis templates load, validate, and reference only tools that exist in Phase 3's registry (stub the registry for now).
- [ ] Loading a fuel-type row from 2023 and one from 2025 maps both to the same canonical fuel entity.

**Human decision:** review the eight mechanisms and their falsifiers. Claude Code will propose wording; you decide whether each mechanism is real, whether the falsifier is fair, and whether any template is missing. This is the single highest-leverage hour you will spend on the project.

## Phase 3 — Deterministic tools and evidence ledger (2 sessions)

Goal: every analytical operation is a pure, replayable function that returns a receipt; the ledger is the only thing the explainer may cite.

**Tool registry (`gridcase/tools/`)** — each tool has signature `fn(inputs: PydanticModel, as_of: datetime) -> Receipt` and is registered with a JSON schema so the planner can select it by name:

| Tool | Inputs | Output (inside the receipt) |
| --- | --- | --- |
| `baseline.expected` | metric, entity, window | expected value and sigma per hour from a seasonal hour-of-week model with a temperature term (fit on trailing 3 years, refit weekly) |
| `anomaly.detect` | metric, entity, window | robust z-score vs `baseline.expected`; flag if \|z\| > 3 for ≥ 2 consecutive hours |
| `variance.plan_vs_actual` | entity, window | demand − forecast in MW and %, by hour |
| `bridge.fuel` | entity, t0, t1 | additive bridge of net-generation change by fuel, ranked by contribution |
| `contribution.subregion` | entity, window | share of the miss by subregion (additive) |
| `regress.temperature` | entity, window | slope, R², and the share of forecast error explained by temperature-forecast error |
| `identity.balance` | entity, window | residual D − NG − TI and its z-score vs history |
| `revision.diff` | series, entity, t\_a, t\_b | rows whose value changed between two `as_of` times |
| `peer.compare` | entity, peers, window | correlation of forecast error with each peer |
| `price.co_movement` | hub, window | LMP vs demand-error correlation and cap-hours count |

**Receipt (`gridcase/ledger/schema.py`):** `receipt_id` (content hash), `tool`, `inputs`, `as_of`, `data_version` (max `observed_at` read), `output`, `effect_size`, `runtime_ms`, `sql` (the compiled query, for audit). Stored in `ledger(case_id, receipt_id, ...)` append-only.

**Exit test `tests/exit/test_phase_3.py`:**

- [ ] Property test: every tool called twice with identical `(inputs, as_of)` returns byte-identical receipts.
- [ ] `bridge.fuel` contributions sum to the total generation change within 0.1%; `contribution.subregion` shares sum to 100% ± 0.5%.
- [ ] `anomaly.detect` on ERCOT 2021-02-15 flags the demand-forecast miss; on 2023-05-01 it flags nothing.
- [ ] `regress.temperature` on ERCOT summer 2023 yields R² ≥ 0.6 between temperature and demand.
- [ ] A receipt's `sql` field, re-executed against the store at its `as_of`, reproduces its `output`.
- [ ] Baseline models fit for all four regions in under 2 minutes total on a laptop.

## Phase 4 — Planner, validator, executor, verify loop, case memory (2 sessions)

Goal: the LLM plans and explains; everything between is deterministic and checkable.

**Case lifecycle:** `detected → planned → executing → concluded → action_taken → rechecking → closed | reopened`. State lives in `cases(case_id, entity, metric, opened_at, as_of, state, plan_json, conclusion_json)`.

**Planner (`gridcase/planner/`):** given the anomaly receipt, the ontology, the applicable hypothesis templates, and the top-3 similar closed cases from case memory, the LLM returns an `InvestigationPlan`:

```python
class ToolStep(BaseModel):
    step_id: str
    tool: str                 # must exist in registry
    inputs: dict
    hypothesis_ids: list[str] # which hypotheses this step tests
    depends_on: list[str] = []

class InvestigationPlan(BaseModel):
    case_id: str
    as_of: datetime
    hypotheses: list[str]     # template ids, ordered by prior
    steps: list[ToolStep]     # a DAG
    budget_steps: int = 12
```

Use structured output; reject and re-prompt on schema failure, max 3 attempts, then fall back to the template's default test order.

**Validator (`gridcase/planner/validate.py`)**, pure rules, no LLM: `H0_DATA_ARTIFACT` present and first; ≥ 2 hypotheses; every tool in registry; every entity in ontology; DAG acyclic; steps ≤ budget; every hypothesis has ≥ 1 step that can falsify it.

**Executor:** topological run; each step writes a receipt; after each step, apply falsifiers and drop refuted hypotheses; skip steps whose hypotheses are all refuted.

**Explainer + verifier (`gridcase/explain/`):** the LLM writes the conclusion as a list of `Claim(text, receipt_ids, tier)` with `tier ∈ {governed, derived, inferred}`. The verifier drops any claim whose receipts don't exist or whose stated numbers don't appear in those receipts (±1% tolerance), and forces `tier=inferred` on any claim with no receipt. Confidence per surviving hypothesis = calibrated from prior × evidence (a simple logistic on falsifier margins is enough for v1).

**Action (`gridcase/actions/`):** exactly one action type in v1: `publish_case`, which writes the case file and schedules a re-check at `now + max(lag_hours of series used)` via an MCP Task. Actions are allow-listed and dry-run by default.

**Verify:** the re-check re-runs every receipt at the new `as_of`, diffs outputs, and marks each hypothesis `confirmed | refuted | superseded`. A closed case updates `hypotheses.yaml` priors for that BA and season (Beta-Binomial update).

**Exit test `tests/exit/test_phase_4.py`:**

- [ ] Uri replay at `as_of=2021-02-15T12:00Z` produces a valid plan on the first or second attempt, runs ≤ 12 steps, and concludes with `H2_SUPPLY_SHORTFALL` as top hypothesis and `H4_LOAD_SHED` flagged.
- [ ] Every claim in the Uri conclusion cites ≥ 1 receipt; a deliberately injected uncited claim is dropped by the verifier.
- [ ] Validator rejects: a plan missing H0; a plan with one hypothesis; a plan with a cycle; a plan naming a tool not in the registry. Four distinct error codes.
- [ ] Re-check at `as_of=2021-02-18T12:00Z` marks H2 `confirmed` and H0 `refuted`, and the case closes.
- [ ] After closing 3 synthetic weather-driven cases for ERCOT summer, `H1_WEATHER_DEMAND` prior for (ERCOT, summer) is higher than before.
- [ ] Total LLM calls per case ≤ 4 (plan, up to 2 retries, explain); log tokens and cost per case.

## Phase 5 — Benchmark (2–3 sessions)

Goal: a reproducible score that says how often the system finds the right cause, how often it cries wolf, and how well it knows when it is unsure. This phase is the portfolio deliverable.

**Replay suite (`bench/replay/`):** 5 documented events, each a YAML case with `entity`, `as_of`, `recheck_as_of`, `ground_truth` (ranked hypothesis ids and the human-written one-line cause), and `sources` (links you read). Uri is the first; you label the other four after Phase 0.

**Fault injector (`gridcase/bench/inject.py`):** takes a clean window and writes a synthetic scenario into a scratch copy of the store with a known cause label. Eight generators, one per hypothesis template: drop hours or add outliers (H0), shift temperature and scale demand by the fitted sensitivity (H1), zero out one fuel for N hours (H2), clamp interchange at its historical max (H3), cap served load below the temperature-implied curve (H4), scale one subregion (H5), suppress the wind proxy (H6), remove a holiday from the forecast baseline (H7). Parameters sampled from ranges you approve; 25 cases per generator = 200 cases; seeds recorded so every case is reproducible.

**Null suite:** 100 clean windows with no injected fault, to measure false alarms.

**Metrics (`bench/score.py`):** root-cause top-1 and top-3 accuracy; false-alarm rate on the null suite; Brier score of the top hypothesis confidence; receipt coverage (share of claims with ≥ 1 receipt, should be 100% by construction, report anyway); trajectory checks (H0 first, ≥ 2 hypotheses tested, mean steps, wasted steps on already-refuted hypotheses); cost and latency per case. Report per hypothesis and per region.

**Leaderboard:** `bench/results/{date}_{model}.json` and a rendered `bench/README.md` table. Run once with the planner model and once with a cheaper model so the site shows the gap. Also run an ablation with the validator turned off, to show how much the architecture is doing versus the model.

**Exit test `tests/exit/test_phase_5.py`:**

- [ ] All 5 replay cases and 300 synthetic cases run end to end without a crash; failures are recorded as scored misses, not exceptions.
- [ ] Injecting a fault with seed S twice produces byte-identical scratch stores.
- [ ] Top-1 accuracy on synthetic cases ≥ 70% and false-alarm rate on the null suite ≤ 10%. If not met, the phase is still complete; record the numbers and open a decision on whether to tune templates or ship as-is.
- [ ] Validator-off ablation runs and its numbers appear in the README table.
- [ ] `bench/README.md` regenerates from results JSON with one command.

**Human decision:** the thresholds above are placeholders. You set the bar you are willing to publish, and you sign off on whether the fault generators are realistic enough that the score means something.

## Phase 6 — MCP server and web UI (2 sessions)

Goal: the system is drivable from any MCP client and readable by a recruiter in 30 seconds.

**MCP server (`gridcase/server/`):** FastAPI, MCP 2026-07-28 stateless core, OAuth 2.0 resource-server metadata stubbed for local use. Tools exposed: `list_open_cases`, `get_case(case_id)`, `investigate(entity, metric, as_of)` (returns an MCP Task handle; the re-check is a second Task), every deterministic tool from Phase 3 read-only, `bench_summary`. No tool exposed that mutates the ontology.

**Web UI (`web/`):** one page, three views.

1. **Map:** US balancing-authority polygons (ERCOT, CAISO, PJM shaded; US48 total in a header tile) colored by current forecast error, updated hourly. Click opens the region's open case or the last closed one.
2. **Case viewer:** left column the hypothesis tree with each node marked supported / refuted / open and its confidence; center the conclusion with claims color-coded by tier (governed / derived / inferred) and each claim expandable to its receipts, including the SQL; right column the plan DAG and the verify diff.
3. **Benchmark:** the leaderboard table, the per-hypothesis confusion matrix, and a "Replay Uri" button that runs the case live with a step-by-step trace.

Host the UI as static files on the same FastAPI service; deploy on the VPS with a systemd unit for the hourly ingest and a second for the server. No auth on the read-only UI; the `investigate` tool requires a token.

**Exit test `tests/exit/test_phase_6.py`:**

- [ ] An MCP client (the Python SDK) can call `investigate` for a synthetic case and poll the Task to completion.
- [ ] The case viewer renders the Uri case with every claim expandable to a receipt whose `sql` is shown.
- [ ] Lighthouse performance ≥ 80 on the map view on a throttled connection.
- [ ] A cold visitor can reach the Uri conclusion in ≤ 3 clicks from the landing page.

## Human intervention checklist

About 10 hours of your time across the whole build, concentrated at phase boundaries. Everything not on this list Claude Code can do unattended inside its exit-test loop.

| When | Decision only you can make | Time |
| --- | --- | --- |
| Before start | Create repo, EIA key in `.env`, pick VPS or laptop for the hourly cron | 30 min |
| Phase 0 gate | Accept Uri as ground truth; choose the other 4 replay events and write their one-line causes | 1 h |
| Phase 2 gate | Approve or rewrite the 8 hypothesis mechanisms and falsifiers; add any missing | 1.5 h |
| Phase 2 gate | Confirm the compiler design mirrors your Gen-2 pattern (Claude Code will drift toward a simpler one) | 30 min |
| Phase 3 gate | Sanity-check the baseline model on one region: does the expected curve look like a grid? | 30 min |
| Phase 4 gate | Read the Uri conclusion cold. Is every claim true? Would you sign it? | 45 min |
| Phase 5 gate | Approve fault-generator parameter ranges; set the publishable accuracy and false-alarm bar | 1.5 h |
| Phase 5 gate | Read 10 random synthetic cases and 10 null cases; override any score you believe is dishonest | 1.5 h |
| Phase 6 gate | Review the landing page copy; decide what the headline number is | 45 min |
| Ongoing | Read `docs/DECISIONS.md` after each phase; reverse anything you disagree with | 15 min per phase |

The two decisions that matter most are the hypothesis templates (Phase 2) and the fault-generator realism (Phase 5). Both determine whether the published score means anything. Do not delegate them.

## Kickoff prompt for Claude Code

Paste this as the first message in a fresh session with plan mode on. It scopes Claude Code to Phase 0 and forces the stop.

```markdown
You are starting the GridCase project. Read CLAUDE.md and docs/SPEC.md in full before doing anything.

Scope for this session: Phase 0 only (Ground-truth gate). Do not touch Phases 1-6.

Steps:
1. Initialize the repo: uv init, Python 3.12, ruff, mypy --strict, pytest, hypothesis, pydantic v2, duckdb, httpx, python-dotenv, pyarrow. Add .gitignore covering .env, data/, and *.duckdb. Commit.
2. Write tests/exit/test_phase_0.py from the five checkboxes in the Phase 0 section of docs/SPEC.md. Run it; confirm it fails because nothing exists yet.
3. Implement scripts/phase0_uri_check.py against the EIA API v2 endpoints listed in the Data sources section. Respondent code is ERCO. Read EIA_API_KEY from .env; never print it. Respect the rate limits. Cache raw JSON under data/raw/uri_2021/.
4. Loop: run the exit test, fix, rerun, until green or until you hit a data problem you cannot resolve (e.g. a series is genuinely missing for the window). If blocked, write what you found to docs/phase0_report.md and stop.
5. When green, write docs/phase0_report.md with: row counts and gaps per series, the peak DF-NG gap and its hour, the fuel bridge from 2021-02-14 12:00 CT to 2021-02-15 12:00 CT, the interchange range, and one paragraph on served load versus true demand during rotating outages.
6. Append a dated entry to docs/DECISIONS.md listing every assumption you made (timezones, how you handled missing hours, any endpoint quirks).
7. Commit and STOP. Report in under 150 words: test status, the peak gap number, the top fuel contributor, and anything you need me to decide.

Do not proceed to Phase 1. Do not create a UI. Do not write any LLM-calling code.
```

For each later phase, the prompt is the same shape: name the phase, point at its exit test, require test-first, require the DECISIONS.md entry, require the stop. The Phase 2 prompt should additionally include a copy of your Gen-2 compiler's public-safe design notes so the compiler pattern carries over.
