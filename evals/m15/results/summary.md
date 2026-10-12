| run | n | answered | Brier | market Brier | skill vs market | log loss | ECE | reward | $ |
|---|---|---|---|---|---|---|---|---|---|
| base_eval | 600 | 588 | 0.2932 | 0.2171 | -0.350 | 0.8311 | 0.2037 | 0.5526 | 3.10 |
| base_rate_eval | 600 | 600 | 0.2428 | 0.2171 | -0.118 | 0.6786 | 0.0440 | 0.7572 | – |
| base_rate_validation | 250 | 250 | 0.2468 | 0.2218 | -0.113 | 0.6867 | 0.0347 | 0.7532 | – |
| base_validation | 250 | 245 | 0.2994 | 0.2218 | -0.350 | 0.8429 | 0.2079 | 0.5686 | 1.32 |
| ckpt-176a00f3-4ce7-5310-aee8-ec3ada524ce8_train_0_sampler_weights_ckpt-39_eval | 600 | 600 | 0.2297 | 0.2171 | -0.058 | 0.6508 | 0.0643 | 0.7653 | 1.28 |
| frontier_eval | 600 | 600 | 0.2068 | 0.2171 | 0.048 | 0.6093 | 0.0431 | 0.6799 | 13.87 |
| market_eval | 600 | 600 | 0.2171 | 0.2171 | 0.000 | 0.6201 | 0.0441 | 0.7829 | – |
| market_validation | 250 | 250 | 0.2218 | 0.2218 | 0.000 | 0.6387 | 0.0929 | 0.7782 | – |
| scripted_market_eval | 600 | 600 | 0.2171 | 0.2171 | 0.000 | 0.6201 | 0.0441 | 0.7829 | 0.00 |
| val_step10_validation | 250 | 250 | 0.2841 | 0.2218 | -0.281 | 0.7771 | 0.2040 | 0.6479 | 0.85 |
| val_step20_validation | 250 | 250 | 0.2425 | 0.2218 | -0.094 | 0.6822 | 0.0892 | 0.7315 | 0.60 |
| val_step30_validation | 250 | 250 | 0.2426 | 0.2218 | -0.094 | 0.6853 | 0.0811 | 0.7434 | 0.50 |
| val_step39_validation | 250 | 250 | 0.2392 | 0.2218 | -0.079 | 0.6766 | 0.0884 | 0.7578 | 0.53 |

| a vs b (same split) | Brier(a) − Brier(b) | 95% CI (cluster bootstrap) |
|---|---|---|
| base_eval vs base_rate_eval | +0.0505 | [+0.0298, +0.0714] |
| base_eval vs ckpt-176a00f3-4ce7-5310-aee8-ec3ada524ce8_train_0_sampler_weights_ckpt-39_eval | +0.0636 | [+0.0452, +0.0823] |
| base_eval vs frontier_eval | +0.0864 | [+0.0651, +0.1080] |
| base_eval vs market_eval | +0.0761 | [+0.0544, +0.0970] |
| base_eval vs scripted_market_eval | +0.0761 | [+0.0544, +0.0970] |
| base_rate_eval vs ckpt-176a00f3-4ce7-5310-aee8-ec3ada524ce8_train_0_sampler_weights_ckpt-39_eval | +0.0131 | [+0.0036, +0.0229] |
| base_rate_eval vs frontier_eval | +0.0360 | [+0.0198, +0.0518] |
| base_rate_eval vs market_eval | +0.0256 | [+0.0138, +0.0375] |
| base_rate_eval vs scripted_market_eval | +0.0256 | [+0.0138, +0.0375] |
| base_rate_validation vs base_validation | -0.0526 | [-0.0830, -0.0201] |
| base_rate_validation vs market_validation | +0.0250 | [+0.0027, +0.0466] |
| base_rate_validation vs val_step10_validation | -0.0373 | [-0.0618, -0.0111] |
| base_rate_validation vs val_step20_validation | +0.0042 | [-0.0161, +0.0222] |
| base_rate_validation vs val_step30_validation | +0.0041 | [-0.0151, +0.0210] |
| base_rate_validation vs val_step39_validation | +0.0076 | [-0.0119, +0.0253] |
| base_validation vs market_validation | +0.0776 | [+0.0451, +0.1125] |
| base_validation vs val_step10_validation | +0.0153 | [+0.0011, +0.0297] |
| base_validation vs val_step20_validation | +0.0568 | [+0.0304, +0.0844] |
| base_validation vs val_step30_validation | +0.0567 | [+0.0294, +0.0844] |
| base_validation vs val_step39_validation | +0.0602 | [+0.0342, +0.0869] |
| ckpt-176a00f3-4ce7-5310-aee8-ec3ada524ce8_train_0_sampler_weights_ckpt-39_eval vs frontier_eval | +0.0229 | [+0.0097, +0.0360] |
| ckpt-176a00f3-4ce7-5310-aee8-ec3ada524ce8_train_0_sampler_weights_ckpt-39_eval vs market_eval | +0.0125 | [+0.0043, +0.0205] |
| ckpt-176a00f3-4ce7-5310-aee8-ec3ada524ce8_train_0_sampler_weights_ckpt-39_eval vs scripted_market_eval | +0.0125 | [+0.0043, +0.0205] |
| frontier_eval vs market_eval | -0.0103 | [-0.0228, +0.0019] |
| frontier_eval vs scripted_market_eval | -0.0103 | [-0.0228, +0.0019] |
| market_eval vs scripted_market_eval | +0.0000 | [+0.0000, +0.0000] |
| market_validation vs val_step10_validation | -0.0623 | [-0.0891, -0.0357] |
| market_validation vs val_step20_validation | -0.0208 | [-0.0395, -0.0035] |
| market_validation vs val_step30_validation | -0.0209 | [-0.0380, -0.0047] |
| market_validation vs val_step39_validation | -0.0174 | [-0.0318, -0.0036] |
| val_step10_validation vs val_step20_validation | +0.0415 | [+0.0230, +0.0598] |
| val_step10_validation vs val_step30_validation | +0.0414 | [+0.0224, +0.0600] |
| val_step10_validation vs val_step39_validation | +0.0449 | [+0.0272, +0.0630] |
| val_step20_validation vs val_step30_validation | -0.0001 | [-0.0084, +0.0079] |
| val_step20_validation vs val_step39_validation | +0.0034 | [-0.0036, +0.0106] |
| val_step30_validation vs val_step39_validation | +0.0035 | [-0.0021, +0.0096] |
