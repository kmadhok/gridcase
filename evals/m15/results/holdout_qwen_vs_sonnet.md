# Qwen3-8B (BD) vs Sonnet 5.5 (BD, Claude Code) (holdout, paired)

## spike_hold (1334 tasks on 1020 markets)

| | Qwen3-8B (BD) | Sonnet 5.5 (BD, Claude Code) | market rule q |
|---|---|---|---|
| Brier | 0.2291 | 0.2142 | 0.2282 |
| Gap to q [95% CI] | +0.0010 [-0.0030, +0.0050] | -0.0140 [-0.0225, -0.0059] | |
| ECE | 0.061 | 0.048 | 0.075 |
| Unanswered | 4 | 1 | |
| Copies q (within 0.01) | 60% | 4% | |
| Direction corr | +0.26 | +0.22 | +0.25 |

Paired Brier difference, Qwen3-8B (BD) − Sonnet 5.5 (BD, Claude Code): **+0.0150 [+0.0069, +0.0233]** (negative = Qwen3-8B (BD) better)

## resolve (538 tasks on 538 markets)

| | Qwen3-8B (BD) | Sonnet 5.5 (BD, Claude Code) | market rule q |
|---|---|---|---|
| Brier | 0.2047 | 0.2069 | 0.2047 |
| Gap to q [95% CI] | +0.0000 [+0.0000, +0.0000] | +0.0021 [-0.0002, +0.0045] | |
| ECE | 0.079 | 0.084 | 0.079 |
| Unanswered | 0 | 0 | |
| Copies q (within 0.01) | 100% | 64% | |

Paired Brier difference, Qwen3-8B (BD) − Sonnet 5.5 (BD, Claude Code): **-0.0021 [-0.0045, +0.0002]** (negative = Qwen3-8B (BD) better)

