# M15 results: backfill, frozen benchmark, harness, and what's left to run

Plan: `docs/plans/2026-10-12_rl_backfill_eval_train.md` (approved "full send", 2026-10-12). Decisions: `docs/DECISIONS.md`, M15 entries.

## Status

**Built and run in this cloud container (all free):**
- the 12-month backfill;
- the point-in-time history tools;
- the frozen eval set;
- the eval harness, run end to end;
- the free baselines;
- the RL trainer, tested offline.

**Local run (laptop, 2026-10-12):**
- `data/` rebuilt from Polymarket. `gridcase rl tasks` reproduced the committed eval set exactly (answers sha256 `106a786b…`, same pools); the built store matches the cloud one (173,188,008 prices, 17.6M trades).
- **Untuned Qwen3-8B: done** ($3.10).
- **Sonnet 5.5: done** ($13.87). The Anthropic credit ran out after 456 tasks; after a top-up the other 144 were rerun with `rl eval --retry-errors`.
- **Training: done** ($41.97 of the $85 allowance). 39 steps (the pilot set the count), validation-selected checkpoint `tinker://176a00f3-4ce7-5310-aee8-ec3ada524ce8:train:0/sampler_weights/ckpt-39` (kept 30 days), evaluated once on the eval split ($1.28).
- Spend: **$64.41 of $150** (`data/rl/spend.jsonl`): training $41.97, small-model evals $8.24, Sonnet $14.21.

## What was built

| Piece | Where | Result |
|---|---|---|
| Census of resolved markets | `gridcase backfill markets` | 62,998 resolved binary, non-recurring markets with ≥ $100k volume that closed 2025-10-11 → 2026-10-11 (73% Sports). Complete: 906 pages. |
| Outcomes | `gridcase backfill resolutions` | 62,998 of 62,998 found on `/v2/resolutions`. Gamma and v2 disagree on 1. |
| Prices | `gridcase backfill prices` | 173,188,008 ten-minute points (token 0) across every market's life. 92k + 34k requests, 0 retries. |
| Trades for task markets | `gridcase backfill trades` | eval + validation: 778 markets, 1.6M taker trades, all complete. Train: 4,905 markets, 16.0M trades, all complete. 17.6M trades in total across all 5,683 task markets. |
| Store | `data/backfill.duckdb` (`gridcase backfill build`) | `hist_*` tables plus the live store's own `trades` schema. Outcomes are in a grader-only table. |
| History tools | `gridcase/tools/hist.py` | `hist.market`, `.move`, `.series`, `.siblings`, `.related`, `.flow`. Point in time; a leakage property test guards them. Real-data latency: most calls well under 0.5 s; `hist.flow` 0.25 s (trades stored in time order); `hist.related` averages 1.5 s, at most 4.3 s. |
| Tasks | `gridcase rl tasks` | Pools: eval 14,800 spike + 18,352 resolve; validation 12,997 + 4,532; train 61,245 + 28,435. |
| Frozen benchmark | `evals/m15/manifest.jsonl`, `answers.sha256` | **Eval: 600 tasks** (400 `spike_hold`, 200 `resolve`) in 600 distinct news clusters, Sports capped at 40%, 51.8% positive. **Validation: 250.** **Train: 6,000** (`train_manifest.jsonl`, 4,921 clusters). |
| Harness | `gridcase/rl/{agent,models,reward,harness}.py`, `gridcase rl eval/report` | The same prompt, six tools plus `submit_answer`, 8-call budget and verifier for every model; a paired cluster bootstrap for comparisons. |
| Trainer | `gridcase/rl/train.py`, `gridcase rl train` | GRPO-style importance sampling, LoRA rank 32 on Qwen3-8B (Tinker), a 3-step pilot, then as many steps as the budget affords; validation-selected checkpoint; early stop; reward-hacking guard. |

## Results (eval split, 600 tasks)

| Row | Brier | Skill vs market | Log loss | ECE | Notes |
|---|---|---|---|---|---|
| Market baseline | **0.2171** | 0 | 0.620 | 0.044 | resolve 0.195, spike_hold 0.228 |
| Base rate | 0.2428 | −0.118 | 0.679 | 0.044 | worse than the market: +0.026 (95% CI +0.014 to +0.038) |
| Scripted agent (harness check) | 0.2171 | 0 | – | – | Calls `hist.move` and `hist.flow`, cites a number from each, submits the market baseline. All 600 answered, 0 malformed, all 1,200 cited claims kept by the verifier. This proves the loop, the tools, the trade data and the verifier work end to end on real data. |
| Qwen3-8B, untuned | 0.2932 | −0.350 | 0.831 | 0.204 | 588 of 600 answered (12 malformed). resolve 0.195 (= market), spike_hold 0.342 (market 0.228). Worse than the market by +0.076 (95% CI +0.054 to +0.097). $3.10. |
| claude-sonnet-5-5 | **0.2068** | +0.048 | 0.609 | 0.043 | 600 of 600 answered (144 rerun with `--retry-errors` after a credit top-up). resolve 0.1948 (market 0.1946); **spike_hold 0.2128 vs market 0.2284** (skill +0.068). Better than the market by 0.010, 95% CI −0.023 to +0.002: not significant. Better than untuned Qwen by 0.086 (CI 0.065 to 0.108). $13.87. |
| Qwen3-8B, RL-tuned | 0.2297 | −0.058 | 0.651 | 0.064 | 600 of 600 answered. resolve 0.197 (market 0.195); spike_hold 0.246 (market 0.228). Better than untuned Qwen by 0.064 (95% CI 0.045 to 0.082) and than the base rate by 0.013 (CI 0.004 to 0.023); worse than the market by 0.013 (CI 0.004 to 0.021) and than Sonnet by 0.023 (CI 0.010 to 0.036). $1.28. |

**What the paid rows say:**
- **RL training worked, but learned a shortcut.** It cut untuned Qwen's Brier by 0.064, and the tuned model beats the base-rate baseline. But on `spike_hold` 387 of its 400 answers are exactly 0.65: it learned "spikes usually hold" and stopped using the evidence, not even the spike's direction (large down-spikes held 93% in train, up-spikes ~45–50%). Tool calls fell from 5.8 to 3.1 per episode.
- **Sonnet 5.5 is the only model at or above the market**, by 0.010 (not significant). Its edge is all on `spike_hold` (0.213 vs 0.228); on `resolve` every model ties the market.
- **Validation over training** (250 tasks; untuned 0.2994, market 0.2218, base rate 0.2468): step 10 0.2841, step 20 0.2425, step 30 0.2426, step 39 0.2392.
- Next levers, if this continues (not run): give the reward a per-task market-relative term so a constant answer earns nothing; put the move's direction and size in the prompt facts; more steps (the run stopped at the pilot's step count with $43 of the training allowance left).

**What the baselines say about the task:**
- The **market price is the bar to beat**: Brier 0.217 overall, 0.195 on `resolve`.
- **Base rates are informative.** In the train split, large downward spikes held 93% of the time (moves of 30 points or more), while upward spikes held 44–51%. A model that learns only this already matches the market baseline on `spike_hold`, so a model has to read the evidence (siblings, related news moves, trade flow) to beat it.

## How to finish (paid, on a machine with keys)

This container has no `.env`, so no `ANTHROPIC_API_KEY` and no `TINKER_API_KEY`. Everything free ran here. The three paid rows need a machine that has both keys. That's the laptop, not the VPS: the VPS is memory-tight and must not be disturbed.

1. **Environment.**
   ```bash
   git checkout claude/polymarket-data-feasibility-anj2kc
   uv sync --group rl          # Tinker SDK + Cookbook (pulls PyTorch; a CPU build is enough)
   ```
   Add `ANTHROPIC_API_KEY=` and `TINKER_API_KEY=` to `.env`, the same file the rest of the repo reads. Never paste them anywhere else.
2. **Data.** `data/` isn't in git. Rebuild it (keyless, about 3 hours at 25 requests a second):
   ```bash
   uv run gridcase backfill markets && uv run gridcase backfill resolutions
   uv run gridcase backfill --rate 25 prices --workers 96 --batch 2000
   uv run gridcase backfill build
   uv run gridcase rl tasks        # must reproduce the committed manifest + answers.sha256
   uv run gridcase backfill --rate 20 trades --needed data/rl/trades_needed.json --workers 64
   uv run gridcase backfill build  # again, now with trades
   ```
   `gridcase rl tasks` refuses to overwrite a committed eval set that differs, and says what changed. If Polymarket has revised history, look at the difference before deciding on `--force`.
3. **Paid evals and training.** Spend is capped at $150 by `data/rl/spend.jsonl`.
   ```bash
   uv run gridcase rl eval --model base --split eval          # untuned Qwen3-8B (Tinker)
   uv run gridcase rl eval --model frontier --split eval      # claude-sonnet-5-5
   uv run gridcase rl train                                   # pilot, then budgeted GRPO; validation-selected; ends with the tuned eval
   uv run gridcase rl report                                  # table + paired bootstraps -> evals/m15/results/summary.md
   ```
   Run the two evals first. They're cheap (about $3 for the base model, roughly $20–30 for Sonnet on 600 tasks), and they tell you whether training has room to help before the training money is spent.

## Caveats

- **Sports dominates the market universe (73%).** It's capped at 40% of eval, but trained behaviour may still be shaped by sports.
- **News clusters come from a tag proxy** (shared specific tags within 2 hours). It misses related events that share no tag, and joins unrelated events that share a theme tag. Splits are by these clusters and by time.
- **One sample per eval task.** The bootstrap intervals cover task sampling, not sampling noise.
- **Gamma's `endDate` is assumed to be the scheduled end known at market creation.** If Polymarket edits it later, `hist.market` would leak a little.
- **Sonnet 5.5's training cutoff isn't stated in the claude-api skill.** Eval starts 2026-07-01, after Opus 5.5's June 2026 cutoff.
- **`hist.flow` counts taker trades only** (the v2 default). Volume figures are therefore about half the two-sided totals.
