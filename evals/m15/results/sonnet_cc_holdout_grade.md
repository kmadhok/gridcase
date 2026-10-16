# M15 holdout grade (data/rl/diagnosis/sonnet_cc/sonnet_cc_holdout.json)

Spike tasks 1334 on 1020 markets; malformed 0.1%. Brier model 0.2142, q 0.2282.

- Primary gap (model − q, unanswered at 0.5): -0.0140 [-0.0225, -0.0059]
- Answered only: -0.0140 [-0.0223, -0.0057]
- Direction corr: +0.22 [+0.16, +0.27]
- Rises: mean p 0.550 vs hold 0.605; corr(p, headroom) -0.64
- Drops: mean p 0.633 vs hold 0.629; corr(p, headroom) -0.70
- sd(p) 0.192; ECE 0.048 vs q 0.075; copy-q share 0.04
- Stretch, 0.25·p + 0.75·q vs q: -0.0085 [-0.0108, -0.0063]
- Resolve (538): Brier 0.2069 vs price 0.2047

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
| STRETCH blend beats q (95% upper bound < 0) | PASS |
