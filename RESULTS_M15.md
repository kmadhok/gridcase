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

"Market" is the market-implied baseline since 2026-10-12: the price for `resolve`; for `spike_hold`, `price_now` after an up-move and `1 − price_now` after a down-move. The earlier spike_hold "market" row was a train bucket base rate; it is kept as **bucket rate**. Paired differences are 95% cluster-bootstrap intervals.

| Row | Brier | Skill vs market | resolve | spike_hold | Notes |
|---|---|---|---|---|---|
| **Market (price-implied)** | **0.2052** | 0 | 0.195 | 0.211 | free |
| claude-sonnet-5-5 | 0.2068 | −0.008 | 0.195 | 0.213 | vs market +0.002 (−0.011 to +0.014): no edge. $13.87 |
| Bucket rate (old "market") | 0.2171 | −0.058 | 0.195 | 0.228 | train hold rate by direction × size |
| Qwen3-8B, RL-tuned | 0.2297 | −0.119 | 0.197 | 0.246 | vs market +0.025 (+0.012 to +0.037); a constant 0.65 on spikes. $1.28 |
| Base rate | 0.2428 | −0.183 | 0.242 | 0.243 | one rate per task kind |
| Qwen3-8B, untuned | 0.2932 | −0.429 | 0.195 | 0.342 | 588 of 600 answered. $3.10 |
| Scripted agent (harness check) | 0.2171 | – | – | – | submits the bucket rate after two cited tool calls; proves the loop end to end |

**Final grading moves to the holdout.** These 600 tasks were studied in the post-mortem, so they now serve development only. `evals/m15/holdout/` holds 1,872 fresh tasks from the same window (1,334 spike_hold, 538 resolve; answers sha256 `42a90f78…`) sharing no news cluster and no market with them; grade with `gridcase rl eval --split holdout`. Resolve came out short of the 666 target because resolve tasks cluster by event and one task per cluster is kept.

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

## Step 2: trainer fixes (2026-10-12, offline; nothing spent)

Decisions: `docs/DECISIONS.md`, "M15 step 2". Code: `gridcase/rl/train.py` (every change is a `TrainConfig` knob and a `gridcase rl train` flag), `gridcase/rl/toy.py` + `experiments/rl_toy/` (the offline proof), `gridcase/rl/diagnose.py` (`gridcase rl diagnose --file …`: do a results file's spike answers follow direction and headroom).

**What changed in the trainer.**
- **Advantages** (`advantage_norm`, default `batch_std`): reward minus the group mean, divided by the step's pooled within-group spread, so a step's advantages have unit RMS however sharp the policy is. The per-group std form the post-mortem asked for is available (`group_std`) but is **biased for a Brier reward**: the samples of a group share the task's outcome, a y = 1 group's rewards spread 2(1 − p) per unit of p and a y = 0 group's 2p, and dividing each group by its own spread erases that ratio, so the expected push is (2P − 1) × spread, toward an extreme rather than toward P. The toy confirms it (answers drift up; seed variance 6× the others). `mean` is the first run's rule.
- **Length normalisation** (`length_norm`, `answer_turn_weight` = 4): every episode's sampled tokens sum to one unit of weight whatever its token count, and the turn that calls `submit_answer` carries 4× per token (prompt tokens stay zero). Tinker's `importance_sampling` loss is a plain sum over tokens, so the advantage tensor is the only place to do this.
- **Spread kept alive** (`kl_coef` = 0.1, `kl_target` = `uniform`): the Cookbook's KL-penalty form (coef × w × (avg − diff) added to the advantages, `tinker_cookbook/rl/metrics.py::incorporate_kl_penalty`) with the per-token weights as the mask. With a uniform reference it is an entropy bonus and costs no extra API calls. KL to the untouched base model is implemented (`--kl-target base`, reference logprobs from `SamplingClient.compute_logprobs(prompt_logprobs_last_n=…)`) but the toy rejects it as the default: the base model's down-move answers (mode 0.35) are wrong, and pulling toward them costs Brier (see the table).
- **Stratified batches** (`stratify`): one spike task from each direction × size bucket first, the rest at random; the resolve share is unchanged. Group size is a knob (`--group-size`); 16 doubles the sampling cost per step.
- **Step count re-planned every step** after the pilot steps from the remaining allowance, the reserve and the running average cost per step (the first run fixed it after 3 steps and left $43 unused).
- **Rollouts and collapse metrics**: one compact row per training episode in `data/rl/train_rollouts.jsonl` (step, task, kind, bucket, p, reward, turns, tool calls, malformed, penalised, advantage, tokens); `train_log.jsonl` gains the fraction of degenerate groups (overall and spike_hold), the spread and distinct count of spike answers, and the planned step count. A **collapse guard** stops training when spike groups are ≥ 50% degenerate, or spike answers take ≤ 3 distinct values, for 3 steps running.

**What the toy showed** (`experiments/rl_toy/results.md`; 200 steps, 5 seeds, same world and seeds for every row). The world: direction × size × headroom contexts with hold rates from the committed base rates and the post-mortem's headroom curve (0.95 → 0.44). The policy: a softmax over answers 0.05 … 0.95 with a shared bias and feature terms (down-move, headroom, size), started from the untuned Qwen's own answer histograms (mode 0.65 after up-moves, 0.35 after down-moves); plus a tool-call-length policy with the harness's over-budget penalty. Groups of 8 share one outcome; Adam with Tinker's defaults; degenerate groups dropped. Best constant on the grid: **0.2183**; the direction/headroom rule: **0.1760**; truth: up-moves hold ~0.47, down-moves ~0.75.

| scheme (toy lr 0.1; lr 0.2 in `results.md`) | Brier | live answers | degenerate groups | mean p up / down |
|---|---|---|---|---|
| old (first run's rule) | 0.2073 ± 0.0019 | 2.8 | 0.54 | 0.59 / 0.62 |
| old + batch_std only | 0.2038 ± 0.0047 | 3.0 | 0.50 | 0.64 / 0.65 |
| old + per-group std | 0.2116 ± 0.0159 | 2.2 | 0.62 | 0.73 / 0.81 |
| old + stratify only | 0.2050 ± 0.0043 | 2.8 | 0.60 | 0.61 / 0.64 |
| old + length norm, answer ×4 only | 0.2071 ± 0.0023 | 3.0 | 0.54 | 0.61 / 0.62 |
| old + KL to base 0.03 / 0.1 / 0.3 | 0.2131 / 0.2562 / 0.2911 | 9 / 10 / 12 | 0.00 | 0.64 / 0.60 … 0.66 / 0.38 |
| old + entropy 0.03 / 0.1 / 0.3 | 0.1995 / 0.2197 / 0.2655 | 11 / 15 / 19 | 0.00 | 0.66 / 0.73 … 0.53 / 0.60 |
| old, groups of 16 | 0.2082 ± 0.0010 | 2.6 | 0.50 | 0.59 / 0.63 |
| new without the entropy term | 0.2048 ± 0.0052 | 3.0 | 0.58 | 0.62 / 0.64 |
| new with KL to base 0.1 instead | 0.2027 ± 0.0032 | 6.2 | 0.15 | 0.62 / 0.63 |
| **new** (batch_std + length + entropy 0.1 + stratify) | **0.1953 ± 0.0016** | 7.0 | 0.22 | 0.64 / 0.72 |
| new, groups of 16 | 0.1924 ± 0.0008 | 8.6 | 0.05 | 0.65 / 0.75 |

- The old scheme reproduces the run's shape: it sharpens to 2–3 live answers within ~30 steps, more than half the groups go degenerate, the answer barely depends on direction (gap 0.05 against a true 0.28), and it stalls. It does not reproduce the full Brier collapse: headroom is a clean feature in the toy, so it still beats the constant by 0.011 (the real model learned nothing of headroom).
- Alone, advantage normalisation, stratification and length weighting change nothing measurable (differences within 1–2 seed standard deviations; Adam normalises per parameter, and the length/answer weighting only matters when parameters are shared, which the toy cannot model). They stay on first-principles grounds. The scale of the advantages sets the scale of the entropy coefficient: with mean-centred advantages 0.03 works (0.1995) and 0.1 is too strong (0.2197); with batch_std, whose advantages keep unit RMS as the policy sharpens, 0.1 works (0.1953) and 0.03 is too weak (0.2044, 41% degenerate). batch_std is kept so the coefficient means the same thing all through training.
- The entropy bonus is the change that matters: groups stop going degenerate and learning continues; with the full new scheme the down-move answer reaches 0.72–0.75 (truth 0.75) and the headroom slope is about 2.5× the old one. The up-move answer stays near 0.64 (truth 0.47) in every variant: the toy learns about half the gap to the rule, not all of it.
- Groups of 16 are better per step (0.1924, 5% degenerate, the lowest seed variance) and about equal per dollar (at 100 steps 0.1977, against 0.1953 for groups of 8 at 200).
- A dropped idea: KL to the base model. Also note the Tinker docs' own advice against per-group std normalisation (Dr. GRPO): agreed, for a different reason than theirs.

**Pilot proposal (needs approval; not run).** Purpose: check the mechanics and the early signal on the real model before spending the remaining ~$43 of the training allowance.

```bash
uv run gridcase rl train --max-steps 6 --eval-every 6 --no-final-eval
```
Config: the defaults (16 tasks × 8 samples per step, LoRA rank 32, lr 4e-5, temperature 1.0, batch_std advantages, length normalisation with the answer turn ×4, entropy bonus 0.1, stratified batches, collapse guard 50% / 3 answers / 3 steps), 6 steps, one validation checkpoint at step 6, no 600-task eval at the end. Cost: the first run's first three steps cost $2.04 each (the early, long episodes; later steps ~$1, $41.97 over 39 steps) and the entropy term adds no API calls, so 6 × $2.04 = **$12.2 worst case** plus the 250-task validation eval ($1.32 for the untuned model, less for a shorter-episode policy): **≤ $13.5, expected $8–11**. A step costs about twice that with `--group-size 16`; use it for the follow-on only if the pilot's degenerate fraction is above 0.3.

Pass/fail, judged on the 250 validation tasks (`gridcase rl diagnose --file evals/m15/results/val_step6_validation.json`) and `data/rl/train_log.jsonl`. For scale, the same command on the eval 600 gives the first run's tuned model direction corr −0.02, headroom corr −0.04, 4 distinct answers, spike Brier 0.246 (best constant 0.242); Sonnet +0.32, −0.52, 54 distinct, 0.213.
- **Pass** (go on to spend the rest, ~20–25 more steps, checkpoints every 10): spike answers follow the prompt (mean p after down-moves exceeds up-moves by ≥ 0.03 and direction corr > +0.1; headroom corr < −0.1); spread alive (≥ 8 distinct spike answers, p std ≥ 0.05, degenerate spike groups < 0.3 at steps 3–5, no guard stop); validation Brier below the first run's step-10 value (0.284 overall) and spike Brier below the untuned 0.34; 768 rows in `train_rollouts.jsonl`.
- **Fail** (stop, report, do not spend the rest): any of those missing, a guard stop, or verifier drops per episode doubling.
- For the follow-on run, the bar the post-mortem set: validation spike Brier below the constant's 0.2475 by step 20 and heading toward ~0.21 (the market-implied baseline is 0.2176 on validation), with the degenerate spike fraction staying under 0.3.

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
