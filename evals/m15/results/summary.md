| run | n | answered | Brier | market Brier | skill vs market | log loss | ECE | reward | $ |
|---|---|---|---|---|---|---|---|---|---|
| base_eval | 600 | 588 | 0.2932 | 0.2171 | -0.350 | 0.8311 | 0.2037 | 0.5526 | 3.10 |
| base_rate_eval | 600 | 600 | 0.2428 | 0.2171 | -0.118 | 0.6786 | 0.0440 | 0.7572 | – |
| base_rate_validation | 250 | 250 | 0.2468 | 0.2218 | -0.113 | 0.6867 | 0.0347 | 0.7532 | – |
| frontier_eval | 600 | 456 | 0.2119 | 0.2171 | 0.024 | 0.5906 | 0.0509 | 0.4594 | 10.19 |
| market_eval | 600 | 600 | 0.2171 | 0.2171 | 0.000 | 0.6201 | 0.0441 | 0.7829 | – |
| market_validation | 250 | 250 | 0.2218 | 0.2218 | 0.000 | 0.6387 | 0.0929 | 0.7782 | – |
| scripted_market_eval | 600 | 600 | 0.2171 | 0.2171 | 0.000 | 0.6201 | 0.0441 | 0.7829 | 0.00 |

| a vs b (same split) | Brier(a) − Brier(b) | 95% CI (cluster bootstrap) |
|---|---|---|
| base_eval vs base_rate_eval | +0.0505 | [+0.0298, +0.0714] |
| base_eval vs frontier_eval | +0.0813 | [+0.0613, +0.1015] |
| base_eval vs market_eval | +0.0761 | [+0.0544, +0.0970] |
| base_eval vs scripted_market_eval | +0.0761 | [+0.0544, +0.0970] |
| base_rate_eval vs frontier_eval | +0.0309 | [+0.0159, +0.0453] |
| base_rate_eval vs market_eval | +0.0256 | [+0.0138, +0.0375] |
| base_rate_eval vs scripted_market_eval | +0.0256 | [+0.0138, +0.0375] |
| base_rate_validation vs market_validation | +0.0250 | [+0.0027, +0.0466] |
| frontier_eval vs market_eval | -0.0053 | [-0.0163, +0.0060] |
| frontier_eval vs scripted_market_eval | -0.0053 | [-0.0163, +0.0060] |
| market_eval vs scripted_market_eval | +0.0000 | [+0.0000, +0.0000] |
