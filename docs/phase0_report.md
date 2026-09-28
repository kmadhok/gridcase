# Phase 0 report — Winter Storm Uri, ERCOT (ERCO), 2021-02-13 to 2021-02-21

Generated 2026-09-28 22:48 UTC from EIA API v2 `electricity/rto/region-data` and `electricity/rto/fuel-type-data`, respondent `ERCO`, `frequency=hourly` (UTC). Raw responses cached in `data/raw/uri_2021/*.json`.

## Coverage per series (216 expected hours, 2021-02-13T00Z to 2021-02-21T23Z)

| Series | Rows | Missing | Missing hours (UTC) |
|---|---|---|---|
| D | 216 | 0 | none |
| DF | 216 | 0 | none |
| NG | 216 | 0 | none |
| TI | 216 | 0 | none |

## Peak forecast-minus-generation gap (DF − NG)

On 2021-02-15 (Central time) the peak gap was **29,650 MW** at 2021-02-15 20:00 CT (2021-02-16T02Z): DF 73,149 MW vs NG 43,499 MW. Peak over the whole window: 29,650 MW at 2021-02-15 20:00 CT.

## Fuel bridge, 2021-02-14 12:00 CT → 2021-02-15 12:00 CT

| Fuel | t0 MW | t1 MW | Δ MW |
|---|---|---|---|
| natural gas (NG) | 37,811 | 29,575 | -8,236 |
| wind (WND) | 8,159 | 3,185 | -4,974 |
| coal (COL) | 11,145 | 7,896 | -3,249 |
| nuclear (NUC) | 5,139 | 3,785 | -1,354 |
| hydro (WAT) | 137 | 143 | +6 |
| other (OTH) | 14 | 21 | +7 |
| solar (SUN) | 832 | 2,278 | +1,446 |
| **total** | 63,237 | 46,883 | -16,354 |

Largest absolute drop: **natural gas (NG)**, -8,236 MW, 50% of the total generation change.

## Total interchange (TI)

Range -1,197 to 357 MW (EIA sign convention: positive = net export). Max |TI| = 1,197 MW, at 2021-02-13 19:00 CT. ERCOT's DC ties to SPP and Mexico total roughly 1.2 GW, so |TI| ≤ 1,500 MW is expected.

## Served load versus true demand

In this window EIA's "demand" (D) for ERCOT is **served load**, not true demand. Form EIA-930 defines D as "actual demand" as reported by the balancing authority, i.e. the load actually metered on the system; the form makes no allowance for load that was curtailed or could not be served (Form EIA-930 instructions, DATA DEFINITIONS, https://www.eia.gov/survey/form/eia_930/instructions.pdf ). ERCOT entered EEA3 early on 2021-02-15 and ordered firm load shed, rotating outages that became prolonged outages of roughly 20 GW at the peak, continuing until 2021-02-18. EIA's *Today in Energy* note of 2021-02-19, "Extreme winter weather is disrupting energy supply and demand, particularly in Texas" ( https://www.eia.gov/todayinenergy/detail.php?id=46836 ), records that generation "fell sharply once ERCOT began implementing rotating outages at midnight on February 15." Metered demand fell because supply was unavailable while weather-implied demand kept rising. Any analysis of this window must not read the fall in D as a fall in demand; the day-ahead forecast (DF) is the better proxy for what consumers wanted, and DF − NG is the shortfall.
