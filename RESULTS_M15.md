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

**Not run: the three paid rows** (untuned Qwen3-8B, Sonnet 5.5, tuned Qwen3-8B). This container has no `.env`, so there's no `ANTHROPIC_API_KEY` or `TINKER_API_KEY`. That's the plan's one stated stop condition. Each paid row is one command once the keys exist (see "How to finish"). The spend ledger caps the total at $150.

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

## Results so far (eval split, 600 tasks)

| Row | Brier | Skill vs market | Log loss | ECE | Notes |
|---|---|---|---|---|---|
| Market baseline | **0.2171** | 0 | 0.620 | 0.044 | resolve 0.195, spike_hold 0.228 |
| Base rate | 0.2428 | −0.118 | 0.679 | 0.044 | worse than the market: +0.026 (95% CI +0.014 to +0.038) |
| Scripted agent (harness check) | 0.2171 | 0 | – | – | Calls `hist.move` and `hist.flow`, cites a number from each, submits the market baseline. All 600 answered, 0 malformed, all 1,200 cited claims kept by the verifier. This proves the loop, the tools, the trade data and the verifier work end to end on real data. |
| Qwen3-8B, untuned | – | – | – | – | needs `TINKER_API_KEY` |
| claude-sonnet-5-5 | – | – | – | – | needs `ANTHROPIC_API_KEY` |
| Qwen3-8B, RL-tuned | – | – | – | – | needs `TINKER_API_KEY` (and the two rows above, to decide whether training is worth it) |

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
