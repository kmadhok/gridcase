# GridCase

Governed investigation engine for plan-vs-actual metrics, proven on US grid data, scored on a public replay benchmark. Full spec: docs/SPEC.md. Read it before any non-trivial change.

## Stack
- Python 3.12, uv for env and deps, ruff + mypy --strict, pytest + hypothesis
- DuckDB for the bitemporal store (single file, data/gridcase.duckdb); Parquet snapshots in data/raw/
- Pydantic v2 for every plan, receipt, hypothesis, and ontology object
- Anthropic SDK for the planner and explainer only; model name in config, never hardcoded
- FastAPI for the MCP server (MCP 2026-07-28 spec, stateless, Tasks extension for re-checks)
- Web UI: single-page app in web/, Vite + React + MapLibre; no backend beyond the FastAPI service
- Secrets only via .env, loaded with python-dotenv; never print, log, or commit them

## Hard rules
1. The LLM never writes SQL. Metrics compile to SQL via gridcase/semantic/compiler.py only.
2. Every tool in gridcase/tools/ is a pure function of (inputs, as_of) and returns a Receipt. No tool reads data with observed_at > as_of.
3. The explainer emits only sentences that reference receipt ids. Enforce in gridcase/explain/verifier.py; uncited sentences are dropped.
4. Plans are gridcase/planner/schema.py::InvestigationPlan. The validator rejects plans that omit hypothesis H0_DATA_ARTIFACT or test < 2 hypotheses.
5. No new data source, hypothesis template, or action type without a line in docs/DECISIONS.md and a stop for human review.
6. Do not create a chat UI. The case viewer is the UI.

## Workflow
- Work one phase at a time from docs/SPEC.md. Each phase has an exit test in tests/exit/. Loop until it passes.
- Before coding a phase: write the exit test first, run it red, then implement.
- After each phase: append a dated entry to docs/DECISIONS.md listing every judgment call you made, then STOP and ask for review.
- Commit at every green test with a conventional-commit message. Never force-push.
- Keep responses short. Report: what passed, what failed, what you decided, what you need from me.

## Commands
- uv run pytest tests/exit/test_phase_N.py -x
- uv run gridcase ingest --source eia --start 2021-02-13 --end 2021-02-21
- uv run gridcase replay --case uri_2021 --as-of 2021-02-15T12:00Z
- uv run gridcase bench --suite all
- uv run gridcase serve

## Data caveats you must respect
- EIA demand during load-shed events is SERVED load, not true demand. Never conclude "demand fell" during an EEA event without flagging this.
- EIA revises history. Store observed_at on every row; never overwrite, always append.
- Fuel-type categories changed in 2024 Q3 and ERCOT split out batteries in Oct 2024. Map old and new categories in the ontology, not in queries.
- gridstatus scrapers break. Pin the version, cache raw responses, and fail soft.

## Deployment
- Production runs on the VPS (ssh alias `vps`, ubuntu, Tailscale 100.115.94.44), not the laptop. Hourly ingest and the server are user systemd units there; the VPS holds the canonical data/gridcase.duckdb and data/raw/. The laptop is for development and uses a copy.
- The VPS has its own .env; secrets are never copied between machines. The T3, Hermes and podcast work already on the host must not be disturbed. Do not change its SSH host keys, T3 state or existing timers.
- Live polling captures EIA revisions; backfilled history cannot. Start the hourly poller on the VPS as soon as the Phase 1 schema exists.
- DuckDB allows one writer per file. Ingest and server must not hold the file open for writing at the same time; decide the process model in Phase 1.
- Slow, network-bound tests (full backfill) run on the VPS, not in the laptop exit-test loop.
