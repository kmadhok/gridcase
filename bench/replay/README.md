# Replay cases

Five documented events. Each becomes a YAML case in this directory in Phase 5. The
ground-truth rankings and one-line causes below were drafted by Claude and accepted by the
human at the Phase 0 gate (2026-09-28); `as_of` values are drafts to be checked against the
actual timeline when the YAML is written. Hypothesis ids refer to `ontology/hypotheses.yaml`.

| id | Event | Entity | as_of (draft) | recheck (draft) | Ranked truth | One-line cause |
|---|---|---|---|---|---|---|
| `uri_2021` | Winter Storm Uri | ERCOT | 2021-02-15T12:00Z | 2021-02-18T12:00Z | H2 > H4 > H1 | Gas supply and plant freeze-offs cut generation ~16 GW while cold drove demand far above forecast; ~20 GW firm load shed made reported demand served load |
| `eclipse_2024` | Solar eclipse | ERCOT, CAISO | 2024-04-08T20:00Z | 2024-04-10T12:00Z | H6 > H7 (small miss; hard negative) | Solar output fell ~90% along the path of totality; the day-ahead forecast largely anticipated it, so the miss is small and a confident root-cause call would be a false alarm |
| `elliott_2022` | Winter Storm Elliott | PJM | 2022-12-24T12:00Z | 2022-12-27T12:00Z | H2 > H1 > H4 | Gas and coal units failed to start or run in extreme cold (~46 GW forced outages) while demand exceeded forecast; PJM avoided load shed, neighbours did not |
| `tx_heat_2023` | Texas heat dome | ERCOT | 2023-06-27T22:00Z | 2023-06-30T12:00Z | H1 > H5 | Sustained record heat pushed demand above forecast; supply held, prices spiked on the evening ramp |
| `caiso_2020` | CAISO rolling blackouts | CAISO | 2020-08-15T04:00Z | 2020-08-18T12:00Z | H4 > H2 > H3 > H1 | Heat-wave demand plus insufficient net-peak supply (solar roll-off, imports unavailable West-wide) forced ~1 GW of load shed |

Known limits: LMP and sub-BA coverage for 2020 CAISO is expected to be thin; tools report
"no data" rather than guessing. Historical cases see revised EIA data (see the replay-leak
entry in `docs/DECISIONS.md`).
