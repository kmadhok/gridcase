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

**What the paid rows say** (corrected 2026-10-12 after a four-way independent analysis; the first write-up was wrong in three places):
- **The spike_hold "market baseline" is not a market price.** `market_baseline` (`gridcase/rl/harness.py:47-51`) returns the train hold rate for the move's direction × size bucket; only on `resolve` is it the price. A market-implied, zero-parameter baseline, P(hold) = `price_now` for an up-move and `1 − price_now` for a down-move, scores **0.2105** on eval spike_hold (bucket rule 0.2284) and **0.2052** overall (old "market" 0.2171).
- **Against that baseline, Sonnet 5.5 has no edge**: +0.002 overall (95% CI −0.011 to +0.014), +0.002 on spike_hold (−0.017 to +0.022). Its apparent edge was the weak bucket rule. The tuned Qwen is worse by 0.025 (CI +0.012 to +0.037) overall and 0.036 on spike_hold.
- **The tuned model is a constant on spikes.** Tuned minus "always 0.65" on spike_hold: −0.0002 (CI −0.002 to +0.001). Its p is uncorrelated with direction (r = −0.02); on down-spikes ≥ 30 points (97% held on eval) it still says 0.65. It calls the same three tools (move, series, flow) on 396 of 400 spike tasks, stopped calling `hist.market`/`hist.siblings` (the question text) by step 30, and writes "65%" into 318 of 400 summaries. Its whole gain over the untuned model is removing the untuned model's anti-correlated bias (it read a price fall as "the move won't hold").
- **It plateaued at step 20.** Validation step 20 vs 10: −0.042 (CI −0.061 to −0.022); 30 vs 20 and 39 vs 30 are indistinguishable from zero. Train reward ended at the expected reward of the constant-plus-echo policy (~0.77). About 0.11–0.18 of the reward gain is penalty avoidance (verifier-penalised episodes 45% → 1%), not forecasting.
- **Why: the training signal designs the collapse in.** Advantages are reward minus group mean (`gridcase/rl/train.py:59-64`), so the update that moves p scales with the policy's own spread (σ²). With no entropy or KL term, the policy sharpened onto its pre-existing modal answer (0.65 was already the untuned model's most common answer); the learning signal for direction-conditional answers fell ~70× as the spread shrank, and fully degenerate groups (all 8 samples equal) are dropped. Training examples per step fell 896 → ~250: half from shorter episodes, half from spike groups going degenerate (~47% of episodes by the end). More steps would not have helped under this objective.
- **The signal was there and is simple.** The direction and price are already in the prompt. A gradient-boosted model on four prompt facts scores 0.206 on spike_hold; the key feature is headroom (distance from `price_now` to the boundary the move points toward; hold rate falls from 0.95 to 0.44 as headroom grows). Trade flow, siblings and price path add at most ~0.004 (not significant). On `resolve` nothing beats the price; every model echoes it.
- **Benchmark power.** 600 tasks detect differences of ~0.010–0.019 Brier. They separate bad from good models, not frontier models from a good market baseline (that would take ~2,000 tasks).
- **Earlier proposals that would not work:** a "market-relative reward" (an additive per-task constant cancels in reward − group mean) and "add direction to the prompt" (it is already there: `agent.py:123`).
- **Changes most likely to help, if continued:** (1) normalise advantages by the group's standard deviation, so a 0.02 Brier gap still gives a unit-size signal; (2) normalise per episode by length and up-weight the answer turn (the probability is 2–3 tokens of hundreds); (3) keep spread alive with a KL term to the base model, and stop on the fraction of degenerate groups; (4) stratify batches by direction × size, groups of 16–32; (5) replace the spike_hold baseline with the market-implied one and report Brier minus it; (6) re-plan step count from the remaining budget each step. A weak leak to fix too: for `resolve`, "closes in about h hours" plus `hist.market`'s scheduled end reveals early closes (did not transfer to eval).
- **Validation over training** (250 tasks; untuned 0.2994, bucket rule 0.2218, base rate 0.2468): step 10 0.2841, step 20 0.2425, step 30 0.2426, step 39 0.2392.

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
