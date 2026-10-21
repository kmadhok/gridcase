# MarketCase

**Live app:** https://www.kanumadhok.com/marketcase (no login)

A governed analytics agent on live Polymarket trades. A product team's KPIs are defined once in a semantic layer, checked every hour against their usual level, and explained when they move, with every number linking to the query that produced it.

## How it works

- **The metric layer is the product.** Each KPI (volume, active/new/returning traders, whale share, sure-thing share, odds flips) is defined once in `ontology/kpis.yaml` and compiled to SQL by `gridcase/semantic/kpis.py`. The model picks KPIs and segments; it never writes SQL.
- **Alarm → investigation.** 18 series, each hour compared with the same hour on up to 28 prior days. On a breach the agent checks for a data problem first, then breaks the change down by category, market, trade size, trader type and odds band.
- **Receipts.** Every tool returns a receipt carrying its SQL. The explainer may only write sentences that cite receipts, and a verifier (`gridcase/explain/verifier.py`) drops any number it can't trace back.
- **Questions from governed evidence only.** The question box and Explore chat answer from a case's receipts or semantic-layer queries the server runs, never from the model's own knowledge.
- **dbt warehouse.** A dbt-duckdb project (`dbt/`) models the same metrics in an hourly dbt and Airflow pipeline with 48 dbt tests; a parity test holds it equal to the semantic layer. Docs and lineage: https://www.kanumadhok.com/marketcase/dbt/
- **Data reality.** Polymarket's public feed is paged every 4 minutes by a collector that records gaps; hours overlapping a gap are never judged.

## Stack

Python 3.12, DuckDB, dbt, Airflow, Pydantic, FastAPI + MCP, React (Vite), Anthropic SDK. Tests: pytest + hypothesis, ruff, mypy --strict.

## Run locally

```
uv sync
uv run gridcase collect      # save live trades
uv run gridcase refresh      # load trades and categorise markets
uv run gridcase serve        # API + UI
uv run pytest
```

Design decisions are logged in `docs/DECISIONS.md`. Not affiliated with Polymarket; it uses public APIs only.
