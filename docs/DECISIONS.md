# Decisions

Dated log of judgment calls. Newest at the bottom. Anything here can be reversed by the human at a phase gate.

## 2026-09-28 — Pre-start

- **Runtime host.** Production (hourly ingest, DuckDB store, FastAPI/MCP server, hourly cron) runs on the existing OVH VPS, not the laptop. Reason: the user wants no dependence on their device, and revision capture requires a poller that is always up. Deployment rules are in CLAUDE.md. Phase 6's "deploy on the VPS" moves earlier for the poller alone.
- **Replay leak caveat (accepted by human 2026-09-28).** EIA serves only the latest value of each data point; there is no vintage API. Backfilled history therefore has a single `observed_at` (ingest time) and cannot support leak-free replay as the spec assumes. Proposed handling: (a) store the real ingest time; (b) for replay, derive a synthetic `observed_at = valid_time + series.lag_hours` for rows whose only known vintage is the backfill; (c) mark such rows `vintage = synthetic` so `revision.diff` and H0 report "no revision history" rather than "no revisions"; (d) state in the benchmark README that historical replay sees revised data, and that revision-based evidence is only available for cases opened after live polling started. The Phase 4 re-check test ("H0 refuted, H2 confirmed") will pass trivially on history; note it as such.
- **Benchmark honesty.** Fault generators are derived from the hypothesis templates, so the synthetic top-1 score partly measures self-agreement. Report synthetic and real-replay scores separately; the 5 real events are the headline.
- **Price data coverage (open).** H2/H3/H4 and `price.co_movement` depend on historical LMP via gridstatus. Coverage for 2020–2024 events is unverified; check before Phase 2 locks the templates.
- **Timezones.** Store and count rows in UTC. Convert to local time only for local-hour comparisons the spec states in CT (e.g. the Uri fuel bridge). EIA `region-data` is requested with `frequency=hourly` in UTC.
- **Weekly baseline refit.** To keep tools pure in `(inputs, as_of)`, the refit date is derived from `as_of` (most recent Monday 00:00 UTC ≤ as_of), never from the wall clock.
- **Routing.** Per the user's global policy, mechanical implementation from a fixed spec is delegated to Codex and verified here; design, data-correctness and anything touching secrets stays with Claude.

## 2026-09-28 — Phase 0 (ground-truth gate)

- **Time basis.** All API calls use `frequency=hourly`, which EIA serves in UTC; parquet `period` is tz-aware UTC. The 216-hour window is 2021-02-13T00Z..2021-02-21T23Z. The spec's "CT" instants (fuel bridge at 12:00 CT, "peak on 2021-02-15") are evaluated in US/Central (CST, UTC−6); the 02-15 peak is taken over UTC hours whose Central date is 02-15.
- **Result summary.** 216/216 hours for D, DF, NG, TI (no gaps). Peak DF−NG on 02-15 CT = 29,650 MW at 20:00 CT (DF 73,149 vs NG 43,499). Fuel bridge 02-14 12:00 → 02-15 12:00 CT: total −16,354 MW; NG −8,236 (50%), WND −4,974, COL −3,249, NUC −1,354; SUN +1,446. TI range −1,197..+357 MW. This is the current (revised) EIA view of the event, not the vintage that was visible during the storm; see the replay-leak entry above.
- **Missing hours.** None occurred, so the "≤ 4 missing, each listed in the report" rule is implemented but untested against real gaps. Missing hours would be reindexed to the 216-slot grid and listed as `YYYY-MM-DDTHH` (UTC) in the coverage table.
- **Served-load citation.** No EIA page states in plain words that D is "served load" during outages. The report cites (a) Form EIA-930 DATA DEFINITIONS, which define D as "actual demand" reported by the BA with no allowance for curtailed load, and (b) *Today in Energy* 2021-02-19 (id=46836) for the rotating outages starting midnight Feb 15. The `gridmonitor/about` URL is not a valid page and is not cited. The "≈20 GW peak load shed" and "until 02-18" figures are from ERCOT's public statements, not from EIA; treat them as context, not evidence.
- **Caching and idempotency.** Raw responses (`rows` array only, no request metadata, no key) are cached at `data/raw/uri_2021/{D,DF,NG,TI,fuel}.json` and reused on rerun, so the exit test runs offline after the first pull. Delete the JSON to force a refetch. `data/` is gitignored; a fresh clone must run the script once with a key.
- **Rate limiting.** 0.25 s minimum spacing between requests; 429/5xx and transport errors back off 2^n s up to 5 attempts. Only 5 requests are needed for this window; paging at 5,000 rows is implemented but not exercised.
- **Duplicates.** `drop_duplicates` on `(period[, fueltype])` keeps the first row. None were observed.
- **Test scope.** The exit test reads artifacts only; it does not call the API. Interpreted "216 hourly rows exist" as a 216-slot grid with ≤ 4 nulls, not literally 216 non-null rows.
- **Fuel-type categories.** February 2021 rows use the pre-2024 codes (COL, NG, NUC, OIL, OTH, SUN, WAT, WND); no BAT rows. Category mapping is deferred to the Phase 2 ontology as the spec requires.
- **Static analysis.** ruff (E,F,I,UP,B,SIM, line 100) and mypy --strict pass. pandas-stubs makes `idxmax`/`sum` return `Hashable`/`complex`; worked around with explicit numpy conversions rather than `# type: ignore`.
- **Routing.** Written inline rather than via Codex: the script reads the secret and is the data-correctness gate for the whole project.
