# MarketCase (repo: gridcase)

A governed analytics agent for Polymarket: a few KPIs a product team owns are defined once in a semantic layer; every hour each is checked against its usual level; when one moves outside its range the agent investigates why (which market, category, trade size, trader type or odds band moved) and writes an answer where every number links to its query. A PM can also ask questions. Current plan: docs/plans/2026-09-29_polymarket_pivot.md. The power-grid version (original spec docs/SPEC.md) is kept at tag `v1-power-grid`.

## Stack
- Python 3.12, uv for env and deps, ruff + mypy --strict, pytest + hypothesis
- DuckDB store (single file, data/gridcase.duckdb): trades, markets, collector gaps, cases, ledger. Raw collector output (gzip JSONL) in data/raw/polymarket/
- Pydantic v2 for every plan, receipt, driver, KPI and ontology object
- Anthropic SDK for the planner, explainer and question box only; model names in config, never hardcoded
- FastAPI + MCP (2026-07-28 spec, stateless) in one service; web UI in web/ (Vite + React), served by the same service
- Secrets only via .env, loaded with python-dotenv; never print, log, or commit them

## Hard rules
1. The LLM never writes SQL. KPIs compile to SQL via gridcase/semantic/kpis.py only, from ontology/kpis.yaml.
2. Every tool in gridcase/tools/ is a pure function of (inputs, as_of) and returns a Receipt. No tool reads trades collected after as_of (`trades_as_of`).
3. The explainer emits only sentences that reference receipt ids. Enforce in gridcase/explain/verifier.py; unsupported numbers are dropped, uncited sentences are marked interpretation.
4. Plans are gridcase/planner/schema.py::InvestigationPlan. The validator rejects plans that do not test the mandatory data check (D0_DATA_PROBLEM) first, or test < 2 drivers.
5. No new data source, KPI, driver, or action type without a line in docs/DECISIONS.md and a stop for human review.
6. No free-form chat UI. The case viewer is the UI. A question box may answer only from a case's evidence (plus read-only checks run at the case's as_of), and every answer passes through gridcase/explain/verifier.py (user decision 2026-09-29).

## Workflow
- Work one phase at a time from the current plan. Each phase has an exit test in tests/exit/ (test_mN.py). Write it first, run it red, then implement; loop until it passes.
- After each phase: append a dated entry to docs/DECISIONS.md listing every judgment call (tag `REVIEW:` where the user should look), then continue (the user reviews async).
- Commit at every green test with a conventional-commit message. Never force-push.
- Keep responses short and in plain English for the user: what passed, what failed, what you decided, what you need.

## Commands
- uv run pytest tests/exit/test_mN.py -x
- uv run gridcase collect            # save live trades (runs as the gridcase-collector service)
- uv run gridcase refresh            # load collected trades + categorise markets once
- uv run gridcase investigate --kpi volume_usd --segment category=Sports [--as-of ...]
- uv run gridcase serve

## Data caveats you must respect
- Polymarket's public trades feed reaches back only ~10,000 trades (~11 minutes) and is CDN-cached for 5 minutes: history exists only because the collector saves it (10,000-trade pages every 4 minutes). Never assume a backfill is possible.
- Our record starts 2026-09-29 19:37 UTC. "New trader" means new to our record; alarms on it wait 7 days.
- Hours overlapping a collector gap are never judged, and never used as history.
- Markets missing from the metadata API are categorised by title keywords (ontology/categories.yaml), else Other.

## Deployment
- Production runs on the VPS (ssh alias `vps`, ubuntu, Tailscale 100.115.94.44), not the laptop. Two user systemd units: gridcase-collector (raw files only) and gridcase-server (the only DuckDB writer: loads trades every 5 minutes, hourly watch, follow-ups, MCP + API + UI on port 8710). `deploy/install.sh` installs both. The laptop is for development.
- The VPS has its own .env. Copying the laptop .env there is allowed only with `scp` on the user's say-so (2026-09-28); its contents never pass through Claude's context, output, or logs. The T3, Hermes and podcast work already on the host must not be disturbed. Do not change its SSH host keys, T3 state or existing timers.
- Public hosting will be under the user's domain kanumadhok.com (not scheduled).
