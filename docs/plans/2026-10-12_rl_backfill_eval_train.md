# M15: backfill → frozen eval set → RL-tune Qwen3-8B (full send)

Status: **approved 2026-10-12 by the user ("full send": execute every phase without checking in)**. Execute every phase without checking in. Stop only on a hard blocker: a missing API key, the $150 spend cap, or a conflict with a CLAUDE.md hard rule that no reading of this plan resolves. Record each blocker in `docs/DECISIONS.md` and in the final report.

## Context

`FEASIBILITY.md` (branch `claude/polymarket-data-feasibility-anj2kc`) found enough gradable events to RL-tune a small model:
- **Spike tasks:** about 21k independent spike clusters per 6 months (≥ $100k markets, ≥ 10 points in 6 h), with a 58/42 held/reverted split.
- **Brier tasks:** about 23k markets whose outcome was still open at the forecast time.

All of it is backfillable from keyless APIs:
- Gamma for markets and outcomes.
- CLOB `/prices-history` for prices at 1–10 minute fidelity.
- data-api `/v2/trades` and `/v2/resolutions` for trades and settled outcomes.

**Goal:** compare three models on identical, point-in-time, governed tool access and identical tasks:
- the RL-tuned Qwen3-8B (LoRA, on a hosted RL API);
- untuned Qwen3-8B;
- claude-sonnet-5-5.

**Budget:** $150 hard cap across all model spend.

**Decided with the user:**
- Hosted RL API: Tinker, with its docs verified before any code is written.
- Total cap: $150.
- Models: Qwen3-8B against Sonnet 5.5.

**Rules that still apply:**
- The LLM never writes SQL (rule 1).
- Every tool is a pure function of (inputs, as_of) and returns a Receipt (rule 2).
- Every answer passes the verifier (rule 3).
- Rule 5's new-data-source stop is waived by the user's approval, but each phase still gets a DECISIONS line, with `REVIEW:` where the user should look.

**Workflow:** one phase at a time. Write the phase's exit test first in `tests/exit/test_m15.py`, see it fail, implement, loop until green, commit with a conventional message, push to the branch, and never force-push.

**Out of scope but due first:** the collector's move to Data API v2 before 2026-10-24 is a separate queued task. This plan never touches the collector, the server, the VPS or the live store.

**Where it runs:** any dev box with network access (this cloud container works: open egress, 30 GB disk). Not the VPS. Data goes to `data/backfill/` and `data/rl/`, which are gitignored. Small manifests are committed.

## Phase A: setup and decisions (short)

- `docs/plans/2026-10-12_rl_backfill_eval_train.md`: a copy of this plan with the approved status line.
- `docs/DECISIONS.md`: an M15 entry. It covers the new sources (Gamma keyset, CLOB prices-history, data-api v2 trades and resolutions), the waiver of the rule-5 stop and why, and the spend cap.
- `CLAUDE.md` data caveats: replace "Never assume a backfill is possible" with the v2 facts. Per-market trades go back 3 years; prices go back to 2023 at 1-minute fidelity; our own live record still starts 2026-09-29.
- `gridcase/config.py`:
  - Add `claude-sonnet-5-5` and other missing 5.5 prices to `MODEL_PRICES`. Read the `claude-api` skill for current prices first, never from memory.
  - Add `RL_BASE_MODEL` (Qwen/Qwen3-8B) and `FRONTIER_MODEL` (claude-sonnet-5-5) as environment-configured names. Never hardcode model names.
- `gridcase/rl/spend.py`, a persistent spend ledger:
  - Append-only `data/rl/spend.jsonl`: phase, model, tokens, usd.
  - `Spend.reserve(phase, usd)` refuses past the global $150 cap and past the phase allowances: frontier evals $35, small-model evals $15, training $85, reserve $15.
  - Every paid call goes through it. Pattern: `experiments/model_pick/run.py::Spend`, made persistent.
- **Key check:**
  - `ANTHROPIC_API_KEY` and `TINKER_API_KEY` must be in `.env`, loaded with python-dotenv. Never print them.
  - If one is missing, run every free phase (B–E with scripted models), then stop before the paid ones and report.

## Phase B: production backfill (`gridcase/backfill/`, CLI `gridcase backfill …`)

**Code.** Port `experiments/feasibility/` into a typed package (mypy --strict, ruff):
- `client.py`: from `api.py::Api` (disk cache, ≤ 4 requests/s, retry on 429/5xx with Retry-After, cached 400s). Log retries.
- `fetch.py`: from `backfill.py`:
  - `clob_prices` (14-day chunks);
  - `v2_trades` (cursor paging; add a stop-at-timestamp so a walk ends once it is older than needed);
  - `v2_resolutions`;
  - `settled_token0` (UMA `price` or v2 `payouts`, from `probe.py`);
  - `life()` (from `probe.py`).
- `census.py`: from `experiments/feasibility/census.py::row` (Gamma `/markets/keyset`, `order=volumeNum`).
- Categories come from `gridcase/ingest/polymarket_load.py::categorize` (`ontology/categories.yaml`), so backfill categories match live ones. Drop the experiment's own category function.

**CLI.** Subcommands registered like the `warehouse` group in `gridcase/cli.py`:
- `gridcase backfill markets --start 2025-10-11 --end 2026-10-11 --min-volume 100000`: Gamma census → `data/backfill/markets.parquet`. Holds resolved binary, non-recurring markets, with the winner kept apart from tool-visible columns.
- `gridcase backfill resolutions`: `/v2/resolutions` → `resolutions.parquet`, cross-checked against Gamma `outcomePrices`. Drop disagreements and count them.
- `gridcase backfill prices --fidelity 10`: token 0, CLOB, over each market's life → `prices/*.parquet`. About 146k requests, ~13 h. Resumable from the cache; run in ≤ 2 h background chunks, each one a cache-resumable rerun.
- `gridcase backfill trades --markets <task markets>`: only for markets that appear in tasks (Phase D). Walk newest → oldest until `as_of − 7 d`; cap at 500 pages per market. Markets that hit the cap get `flow_available = false`.
- `gridcase backfill build`: builds `data/backfill.duckdb` from the Parquet files. It is written only by this command; tools open it read-only.
  - `hist_markets` (no outcome columns), `hist_prices(condition_id, ts, p)`, `hist_resolutions` (grader-only).
  - A trades-shaped `trades` table plus a `markets` table, with `collected_at = ts`, so the existing `trades_as_of`, the semantic layer and the `kpi.*` / `by.*` tools run unchanged against the backfill.
  - Note in DECISIONS that "new trader" here means new to the backfilled history.

**Exit test** (`test_m15.py::phase_b`), using recorded API responses in `tests/fixtures/backfill/` (a few real cached bodies):
- chunking stays under 15 days;
- a 400 response is cached;
- both resolution formats parse;
- an end date before the start date falls back to the closed time;
- recurring markets are excluded;
- categories match `categorize`;
- `build` produces the tables;
- outcome columns are absent from `hist_markets`.

## Phase C: point-in-time history tools (`gridcase/tools/hist.py`)

New tools registered in `gridcase/tools/registry.py` under a new `HIST_INPUTS` group (Pydantic inputs, fixed SQL, Receipts, `run_tool(...)`). Every query filters on `ts <= as_of` through macros `prices_as_of(t)` and `hist_markets_as_of(t)`; the second hides markets whose start is after `t`.

| Tool | Returns |
|---|---|
| `hist.market` | question, event, category, tags, start, scheduled end. Never the outcome. |
| `hist.series` | price series ≤ as_of at grain 10m/1h/1d, with the last point |
| `hist.move` | change over 1/6/24/72 h windows ending at as_of, with volatility over the last 7 d |
| `hist.siblings` | the same event's other markets and their moves over the same window |
| `hist.related` | other markets with ≥ X-point moves in the ±2 h window that share a specific tag. "Specific" means on ≤ 2% of events, as in `experiments/feasibility/analyze.py::generic_tags`. |
| (existing) `kpi.compare`, `by.size`, `by.trader`, `by.odds`, `by.market` | run against `backfill.duckdb` when `flow_available`; otherwise a Receipt saying "no trade data" |

- `ToolCtx` gets a `source` (live store or backfill) so the same registry serves both. No change to `semantic/kpis.py`.
- **Exit test:**
  - **Leakage property test (hypothesis):** change or delete any data after `as_of` and every tool's output is byte-identical.
  - The outcome never appears in any tool output.
  - Receipts are stable across reruns.

## Phase D: tasks, splits, frozen eval set (`gridcase/rl/tasks.py`)

**Spike detection and clustering.** Port `analyze.py::spikes`, `Series` and `clusters` as-is:
- 10-minute grid;
- base price in [0.05, 0.95];
- 72 h of trading before close;
- 24 h cooldown;
- event clusters within 6 h, news clusters within 2 h on a shared specific tag.

**Task types.** Each is a Pydantic `Task(task_id, kind, condition_id, as_of, prompt_facts, cluster_id, split)`, with answers stored apart in `data/rl/answers.parquet`:
1. `spike_hold`, the main type: X = 10 points, Y = 6 h, market ≥ $100k. "This market moved +Δ in 6 h at `as_of`. Investigate with the tools; give P(the move holds, retaining ≥ 50%, at +72 h)." The answer is retained(72 h) ≥ 0.5.
2. `resolve`: as_of = close − 24 h (and close − 7 d), price in [0.05, 0.95]. "Give P(outcome 0)." The answer is the resolution.

**Splits.** By news cluster and time, with a purge:
- **eval:** as_of from 2026-07-01 (configurable `EVAL_START`, set after the latest training cutoff of the compared models; confirm Sonnet 5.5's cutoff in the `claude-api` skill) through 2026-10-08.
- **validation:** 2026-05-15 → 2026-06-15.
- **train:** before 2026-05-08.
- A task is dropped if its +72 h label window, or its news cluster, crosses a boundary.

**Sizes and quotas:**
- eval: 400 `spike_hold` + 200 `resolve`;
- validation: 150 + 100;
- train: everything left, capped at 4,000 + 2,000;
- at most 1 task per news cluster in eval and validation;
- category quotas, with sports ≤ 40%;
- `trades` backfill then runs for exactly these markets.

**Freeze.** `evals/m15/manifest.jsonl` (task ids, kinds, as_of, condition ids, no answers) and `evals/m15/answers.sha256` are committed. The answers file stays in `data/rl/`. Freezing happens before any model sees an eval task.

**Exit test** (synthetic series built like `tests/polymarket_synth.py`):
- labels on hand-built held and reverted series;
- cooldown;
- cluster ids;
- purge at boundaries;
- no cluster in two splits;
- the manifest has no answer fields;
- the sha256 matches.

## Phase E: harness and baselines (`gridcase/rl/agent.py`, `harness.py`, `reward.py`; CLI `gridcase rl eval`)

**Agent loop.** Model-agnostic and multi-turn tool use, adapted from `gridcase/ask/llm.py::AnthropicAsker`:
- the same system prompt, tool list (Phase C tools, JSON schemas from the Pydantic inputs), budget of 8 tool calls and 6k-token cap on tool outputs for every model;
- the model ends with `submit_answer{probability, summary, claims[{text, receipt_ids}]}`;
- `summary` and `claims` pass `gridcase/explain/verifier.py::verify`: unsupported numbers dropped, uncited sentences marked as interpretation (rule 3).

**Model adapters:**
- `AnthropicAgent`: Sonnet 5.5 through the Anthropic SDK. Read the `claude-api` skill before writing it.
- `TinkerAgent`: Qwen3-8B base or a LoRA checkpoint, through Tinker's sampling client.
- `ScriptedAgent`: for tests.

**Reward and scoring:**
- p is clipped to [0.01, 0.99]; reward = 1 − (p − y)².
- −0.25 if the answer is malformed, the tool budget is exceeded, or the verifier drops more than half of the claims.
- Report Brier, log loss, calibration (10 bins, ECE), and Brier skill against two free baselines:
  - **market price**: for `resolve`, p = the price at as_of; for `spike_hold`, p = the train-split base rate by Δ bucket;
  - **base rate**.
- Also report tool-use stats, verifier drops, cost and latency.

**Statistics.** A paired bootstrap over news clusters (2,000 draws) for each model pair. Pre-registered success: tuned beats untuned with the 95% interval above 0. Report against Sonnet and the market whichever way they fall.

**Runs:**
- `gridcase rl eval --model base|frontier|ckpt:<id> --split eval|validation`, writing results to `evals/m15/results/<model>_<split>.json` (committed).
- 1 sample per task at temperature 0.7 for Qwen3-8B; Sonnet with default settings and 1 sample.
- Run the untuned model and Sonnet on eval now.

**Exit test:**
- reward edge cases;
- a scripted agent reproduces the market baseline exactly;
- the bootstrap on fixed numbers;
- the spend ledger blocks at the cap;
- the verifier gate penalizes uncited numbers.

## Phase F: RL training (`gridcase/rl/train.py`; CLI `gridcase rl train`)

- **Verify first.** Read the Tinker docs and record the exact training and sampling calls and the current prices in DECISIONS before coding, the same way the Polymarket endpoints were verified.
- **Fallback model.** If Tinker doesn't serve Qwen3-8B, use the closest Qwen3 model of 8B or smaller that it does serve. Use it for both the untuned and tuned rows, so both small-model rows come from the same model and sampler, and record the substitution.
- **Method.** GRPO-style, with LoRA rank 32 on Qwen3-8B:
  - 8 rollouts per task (group-normalized advantage);
  - 32 tasks per step, mixing spike_hold and resolve 2:1;
  - multi-turn rollouts through the same agent loop and tools as eval.
- **Pilot.** 3 steps to measure dollars per step, then steps = floor((remaining training allowance − $5) / cost per step), capped at 150.
- **Checkpoints.** Save every 10 steps and score each on the **validation** split (eval is never touched). Keep the best validation Brier.
- **Training data.** Train-split tasks only; trajectories and rewards logged to `data/rl/train_log.jsonl`.
- **Guards:**
  - stop early if validation Brier hasn't improved for 3 checkpoints;
  - stop on a reward-hacking signal: verifier-drop rate up more than 2× or tool calls collapsing to 0;
  - stop at the spend cap.
- **Final.** Score the chosen checkpoint once on eval, writing `evals/m15/results/tuned_eval.json`.
- **Exit test** (offline, no API): rollout serialization, group advantages, step-count arithmetic from the pilot cost, and checkpoint selection on validation only.

## Phase G: results

- `RESULTS_M15.md`:
  - the headline table: Brier, log loss, ECE and skill against the market for tuned, untuned, Sonnet, the market and the base rate, split by task kind and category;
  - bootstrap intervals;
  - spend by phase;
  - what got better (tool use, calibration) and failure examples;
  - leakage checks run;
  - caveats: sports share, tag-proxy clustering, a single eval sample.
- A final DECISIONS entry listing every judgment call, with `REVIEW:` tags.
- Commit and push.

## Files

- **New:**
  - `gridcase/backfill/{client,fetch,census,build}.py`
  - `gridcase/tools/hist.py`
  - `gridcase/rl/{spend,tasks,agent,reward,harness,train}.py`
  - `tests/exit/test_m15.py`
  - `tests/fixtures/backfill/`
  - `evals/m15/`
  - `RESULTS_M15.md`
- **Edited:**
  - `gridcase/cli.py` (backfill and rl groups)
  - `gridcase/tools/registry.py` and `base.py` (`HIST_INPUTS`, `ToolCtx.source`)
  - `gridcase/config.py`
  - `pyproject.toml` (the Tinker SDK dependency)
  - `CLAUDE.md`
  - `docs/DECISIONS.md`
- **Reused unchanged:**
  - `semantic/kpis.py`, `trades_as_of`
  - the `kpi.*` / `by.*` tools
  - `explain/verifier.py`
  - `ingest/polymarket_load.py::categorize`
  - `tests/polymarket_synth.py` patterns

## Verification

- At every phase: `uv run pytest tests/exit/test_m15.py -x`, the full `uv run pytest`, `ruff check`, and `mypy --strict gridcase`. All must be green before each commit.
- **Backfill sanity:** row counts against the census (~63k markets ≥ $100k over 12 months); resolution agreement ≥ 99%; price coverage ≥ 95% of market-minutes on a 50-market spot check.
- **Leakage:** the Phase C property test, plus a spot audit of 20 eval tasks, re-running each tool with the database truncated at `as_of` (outputs must be identical).
- **End to end:** `gridcase rl eval --model base --split validation --limit 10` runs with spend recorded; the market baseline is reproduced exactly by `ScriptedAgent`.
- **Final:** `evals/m15/results/*.json` exists for all five rows; `spend.jsonl` total ≤ $150; `RESULTS_M15.md` numbers regenerate from the results files with `gridcase rl report`.
