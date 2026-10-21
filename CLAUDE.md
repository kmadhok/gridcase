# MarketCase (repo: gridcase)

A governed analytics agent for Polymarket: a few KPIs a product team owns are defined once in a semantic layer; every hour each is checked against its usual level; when one moves outside its range the agent investigates why (which market, category, trade size, trader type or odds band moved) and writes an answer where every number links to its query. A PM can also ask questions. Plans: one per milestone in docs/plans/ (dated file names); M1–M12 are built and deployed, and the latest docs/DECISIONS.md entries describe the current state. A plan whose status line says superseded or proposed is not to be implemented without the user's go-ahead. The power-grid version (original spec docs/SPEC.md) is kept at tag `v1-power-grid`.

## Stack
- Python 3.12, uv for env and deps, ruff + mypy --strict, pytest + hypothesis
- DuckDB store (single file, data/gridcase.duckdb): trades, markets, collector gaps, cases, case_trace (one step-by-step trace per investigation), ledger. Raw collector output (gzip JSONL) in data/raw/polymarket/
- dbt warehouse (dbt/, dbt-duckdb) reads the raw files, not the store, and writes only data/warehouse.duckdb: 8 models (staging, dims, fct_trades, metrics_daily, metrics_hourly_by_category, trader_retention) and 48 dbt tests. Its KPI formulas restate ontology/kpis.yaml and are held equal by a parity test; trader_retention is not a governed KPI
- Pydantic v2 for every plan, receipt, driver, KPI and ontology object
- Anthropic SDK for the planner, explainer and case question box; OpenRouter (cheap open-weight models) for the Explore chat; model names in config, never hardcoded
- FastAPI + MCP (2026-07-28 spec, stateless) in one service; web UI in web/ (Vite + React), served by the same service
- Secrets only via .env, loaded with python-dotenv; never print, log, or commit them

## Hard rules
1. The LLM never writes SQL. KPIs compile to SQL via gridcase/semantic/kpis.py only, from ontology/kpis.yaml.
2. Every tool in gridcase/tools/ is a pure function of (inputs, as_of) and returns a Receipt. No tool reads trades collected after as_of (`trades_as_of`).
3. The explainer emits only sentences that reference receipt ids. Enforce in gridcase/explain/verifier.py; unsupported numbers are dropped, uncited sentences are marked interpretation.
4. Plans are gridcase/planner/schema.py::InvestigationPlan. The validator rejects plans that do not test the mandatory data check (D0_DATA_PROBLEM) first, or test < 2 drivers.
5. No new data source, KPI, driver, or action type without a line in docs/DECISIONS.md and a stop for human review.
6. No free-form chat over the model's own knowledge. The case viewer and the Explore page are the UI. A question box or the Explore chat may answer only from governed evidence: a case's receipts, the page's own data, or semantic-layer queries the model fills in and the server runs (`kpi.series`). Every answer passes through gridcase/explain/verifier.py (user decisions 2026-09-29 and 2026-10-05).

## Workflow
- Work one phase at a time from the current plan. Each phase has an exit test in tests/exit/ (test_mN.py). Write it first, run it red, then implement; loop until it passes.
- After each phase: append a dated entry to docs/DECISIONS.md listing every judgment call (tag `REVIEW:` where the user should look), then continue (the user reviews async).
- Commit at every green test with a conventional-commit message. Never force-push.
- Keep responses short and in plain English for the user: what passed, what failed, what you decided, what you need.

## Commands
- uv run pytest tests/exit/test_mN.py -x
- uv run gridcase collect            # save live trades (runs as the gridcase-collector service)
- uv run gridcase fill-gaps          # fill recorded collector gaps from the feed's history (~a month back)
- uv run gridcase refresh            # load collected trades + categorise markets once
- uv run gridcase investigate --kpi volume_usd --segment category=Sports [--as-of ...]
- uv run gridcase digest [--day YYYY-MM-DD]   # build the morning read (the server does it at 00:30 UTC)
- uv run gridcase serve
- uv run gridcase dbt build          # build and test the local warehouse
- uv run gridcase dbt docs generate
- uv run gridcase warehouse check-sources [--max-age-minutes 15]
- uv run gridcase warehouse publish  # MotherDuck; skips without MOTHERDUCK_TOKEN
- bash orchestration/airflow/setup.sh  # separate Python 3.12 Airflow environment
- orchestration/airflow/.venv/bin/pytest orchestration/airflow/tests -q

## Data caveats you must respect
- The collector reads Polymarket's v2 trades feed (v1 retired 2026-10-24): newest first, 1,000 trades a page, a cursor reaching back about a month, no date filter. Every 4 minutes it pages back to the last saved trade; `collector_state.json` in the raw dir records how far the record is complete, so after a restart or outage it fills the missed time (up to MAX_PAGES, about three days, before writing the rest as a gap). Raw lines keep v1 field names. Older history (a market's whole trade history, prices) is backfillable but lives only in data/backfill/, never in the live store (FEASIBILITY.md, M15).
- Our record starts 2026-09-29 19:37 UTC. "New trader" means new to our record; alarms on it wait 7 days.
- Hours overlapping a collector gap, or past where the record is known complete (the newest trade, or the collector's `complete_to` mark from `collector_marks`, whichever is earlier), are never judged and never used as history: while the collector is down or still catching up, an empty hour is unknown, not zero.
- Markets missing from the metadata API are categorised by title keywords (ontology/categories.yaml), else Other.

## Deployment
- Production runs on kayak (ssh alias `kayak`: WSL2 Ubuntu on a Windows PC, user kanum, Tailscale 100.125.186.6, repo at ~/Documents/Claude/Projects/gridcase), not the laptop. It moved off the OVH VPS on 2026-10-15; on 2026-10-16 the VPS record (2026-09-29 → 2026-10-16 15:31 UTC: store with cases/digests, raw trades, gaps) was merged into kayak, gaps recomputed as times neither collector covered. The VPS units are disabled; its copy of the data is left in place. User systemd units: gridcase-collector (raw files only), gridcase-server (the only writer to the app's DuckDB store: loads trades every 5 minutes, hourly watch, follow-ups, MCP + API + UI on port 8710), and gridcase-airflow (the separate dbt warehouse). `deploy/install.sh` installs them and retires the gridcase-dbt timer. The laptop is for development.
- Airflow runs in its own `orchestration/airflow/.venv` (3.3.2, Python 3.12), with its UI only on Tailscale at http://100.125.186.6:8711. The hourly :20 DAG checks source freshness, runs dbt build, generates docs, then publishes three marts to MotherDuck. No catchup, one active run, two retries; standalone uses SQLite under `~/.local/share/marketcase-airflow`.
- kayak is shared (standing Claude/Codex tmux sessions, other projects; ~11 GB for WSL) and the Windows PC must stay awake for production to stay up. The Airflow unit has MemoryMax=2600M (Airflow idles near 1 GB), parallelism 2, one API worker and Nice=10; `dbt/profiles.yml` caps DuckDB at 1 GB (build peak ~1.3 GB, spills to disk). If an OOM kill appears, lower DuckDB's limit before raising the cap.
- MotherDuck/Hex publishing is unused today: no `MOTHERDUCK_TOKEN` is set, so the publish task is a no-op that skips successfully. If the token is ever added, it lives in the kayak repo's `.env`, loaded by the gridcase CLI; never put it in units or DAGs. `MARKETCASE_PUBLISH_TARGET` defaults to `md:marketcase`; a file path supports local verification. Hex connects to MotherDuck database `marketcase` (see `docs/hex.md`).
- kayak has its own .env, copied from the laptop with `scp` on the user's say-so (2026-10-15); its contents never pass through Claude's context, output, or logs. The other work on kayak (tmux sessions, ~/projects) must not be disturbed. The old OVH VPS deployment is retired (units disabled 2026-10-16); the laptop key and a kayak key (~/.ssh/id_ed25519_vps) are authorized there.
- Public at https://www.kanumadhok.com/marketcase (Vercel rewrite → marketcase.kanumadhok.com → Cloudflare Tunnel unit gridcase-tunnel → the server). MCP is never public; the question box is capped at $2/day.
