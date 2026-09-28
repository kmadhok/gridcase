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
