"""M15 backfill: closed markets, outcomes, price series and trades from Polymarket's keyless
read APIs, written to data/backfill/ (Parquet) and built into data/backfill.duckdb.

Never touches the live store (data/gridcase.duckdb), the collector or the server."""
