# Plan: fewer false alarms, better supply-shortfall calls

Status: proposed 2026-09-29, awaiting go. Owner: Claude (Fable). Cost: ~1 day of work + one
3-hour benchmark run on the VPS. Baseline to beat (`bench/results/2026-09-29_fake.json`):
false alarms **24/100**, synthetic top-1 **48%** (H2 **16%**), replay top-1 **3/5**.

## Why the numbers are what they are

1. **The alarm is tuned to a synthetic world.** `anomaly.detect` flags an hour when the miss
   (actual minus forecast) is more than 3 "typical spreads" from the model of a normal hour,
   for 2 hours running. The spread (MAD per hour-of-week, `baseline.py:195-202`) is a robust
   estimate that *ignores* the fat tail real forecast errors have, and forecast errors are
   strongly autocorrelated, so "2 hours running" adds almost nothing. On seven years of
   ERCOT data, ordinary days trip it: 24% of clean windows.
2. **Weather is judged against our own model, not the forecast.** The H2 falsifier
   (`ontology/hypotheses.yaml`) refutes a supply shortfall when
   `regress.temperature.share_explained_window > 0.5 and sign_consistent_window == 1`, i.e.
   when the miss correlates with the *thermal surprise vs. a 14-day climatology*. A day that
   is simply hotter than the last two weeks yields a high share even when the operator's
   forecast already priced the heat in. The injected 12% fuel loss on such a day is read as
   weather. Result: H2 top-1 16%, and Elliott (PJM) / CAISO 2020 called "weather" first.

## What changes

### A. Calibrate the alarm per grid on real history (`gridcase/tools/anomaly.py`, `baseline.py`)

- Keep the hour-of-week + temperature model for the *expected* value; replace the per-bucket
  MAD sigma with an **empirical quantile threshold per BA**: the |residual| at the 99.5th
  percentile over the training years (three years before the refit date, same leak-free
  refit rule). Store it in the fit next to sigma; report both `z` (unchanged, for receipts)
  and a new `exceedance` (residual ÷ q99.5).
- Flag rule becomes: `exceedance > 1` for **3 consecutive hours**, or `exceedance > 2` for one
  hour (a single catastrophic hour must still count).
- Tunables live in `ontology/detector.yaml` per BA (quantile, min run, single-hour multiple);
  defaults above. No code change to the falsifier DSL: templates that read `max_abs_z` keep
  working; the H2/H3/H6 templates gain `anomaly.detect.exceedance` where they currently use
  `max_abs_z < 3`.

### B. Judge weather against the operator's forecast (`gridcase/tools/regression.py`, templates)

- New receipt fields on `regress.temperature`: `forecast_weather_share` and
  `forecast_weather_sign` = the R² and sign of the *forecast error* on the thermal surprise
  **relative to what the forecast implied**, where "what the forecast implied" is the
  temperature that best explains the day-ahead forecast itself under the fitted demand model
  (invert the fit per hour; falls back to climatology when the fit is poor, `r2 < 0.2`).
  Plain English: did the weather turn out different from what the forecaster assumed?
- H1's `explains` and H2/H4's "weather-explained" clauses switch to these fields. H1 is then
  supported only when the weather genuinely surprised the forecaster in the right direction;
  H2 is refuted by weather only on the same basis.

### C. Held-out evaluation, then the switch

1. **Before touching thresholds**, split: tune A on the 100 null windows and 200 synthetic
   cases with seed 1; evaluate on fresh seeds (2) and on the five replay events, which are
   never used for tuning. Record both in `bench/results/` with `_seed2` suffixes.
2. Run the full benchmark once with A only, once with A+B (three hours each, snapshot store,
   server untouched). Ablation table goes into `bench/README.md` automatically.
3. Acceptance: false alarms ≤ 10/100 on seed 2; H2 top-1 ≥ 50%; replay top-1 ≥ 3/5 with Uri
   still H2 first. If replay drops, the threshold is too tight: back off the quantile to 99.0
   and re-run once. If it still drops, keep the old detector and report.

## What does not change

- No new data source, hypothesis or action (rule 5). Receipts stay pure functions of
  (inputs, as_of). The z-score stays in every receipt so old cases remain comparable.
- The demand baseline itself (hour-of-week + temperature) stays; it is the *threshold* and
  the *weather comparison* that change.

## Tests (written first)

- `test_anomaly_quantile_threshold_is_leak_free`: the q99.5 uses only residuals before the
  refit date.
- `test_alarm_needs_three_hours_or_one_catastrophic_hour`: property test on synthetic runs.
- `test_forecast_weather_share_is_zero_when_forecast_already_priced_the_heat`: a hot day whose
  forecast matches demand yields `forecast_weather_share ≈ 0`; the same day with a 12% fuel
  loss yields H2 supported, H1 refuted.
- Exit: `tests/exit/test_phase_5.py` soft targets updated to the acceptance numbers above.

## Risks

- **Regime drift**: a quantile from 2019–2021 may be loose or tight for 2024; mitigated by the
  Monday refit rule (rolling three-year window).
- **PJM/CAISO with thinner temperature coverage**: the inverse-fit fallback to climatology.
- **Effort**: B is the larger piece; A alone may already meet the false-alarm target, so A
  ships first and B is re-scoped if A+B's gain on H2 is under 10 points.
