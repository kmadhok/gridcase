# M15 holdout grade (evals/m15/results/base_holdout.json)

Spike tasks 1334 on 1020 markets; malformed 0.3%. Brier model 0.2291, q 0.2282.

- Primary gap (model − q, unanswered at 0.5): +0.0010 [-0.0030, +0.0050]
- Answered only: +0.0007 [-0.0032, +0.0046]
- Direction corr: +0.26 [+0.20, +0.32]
- Rises: mean p 0.575 vs hold 0.605; corr(p, headroom) -0.93
- Drops: mean p 0.675 vs hold 0.629; corr(p, headroom) -0.92
- sd(p) 0.191; ECE 0.061 vs q 0.075; copy-q share 0.60
- Stretch, 0.25·p + 0.75·q vs q: -0.0009 [-0.0019, +0.0001]
- Resolve (538): Brier 0.2047 vs price 0.2047

| check | result |
|---|---|
| PRIMARY non-inferior to q (95% upper bound <= +0.015) | PASS |
| direction corr > 0, CI excludes 0 | PASS |
| up mean p within 0.07 of hold rate | PASS |
| down mean p within 0.07 of hold rate | PASS |
| corr(p, headroom) < 0 for rises | PASS |
| corr(p, headroom) < 0 for drops | PASS |
| sd(p) >= 0.12 | PASS |
| ECE <= ECE(q) + 0.01 | PASS |
| malformed <= 5% | PASS |
| STRETCH blend beats q (95% upper bound < 0) | FAIL |
