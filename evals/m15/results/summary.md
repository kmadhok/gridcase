| run | n | answered | Brier | market Brier | skill vs market | log loss | ECE | reward | $ |
|---|---|---|---|---|---|---|---|---|---|
| base_rate_eval | 600 | 600 | 0.2428 | 0.2171 | -0.118 | 0.6786 | 0.0440 | 0.7572 | – |
| base_rate_validation | 250 | 250 | 0.2468 | 0.2218 | -0.113 | 0.6867 | 0.0347 | 0.7532 | – |
| market_eval | 600 | 600 | 0.2171 | 0.2171 | 0.000 | 0.6201 | 0.0441 | 0.7829 | – |
| market_validation | 250 | 250 | 0.2218 | 0.2218 | 0.000 | 0.6387 | 0.0929 | 0.7782 | – |

| a vs b (same split) | Brier(a) − Brier(b) | 95% CI (cluster bootstrap) |
|---|---|---|
| base_rate_eval vs market_eval | +0.0256 | [+0.0138, +0.0375] |
| base_rate_validation vs market_validation | +0.0250 | [+0.0027, +0.0466] |
