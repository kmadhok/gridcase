# ruff: noqa: E501  (SQL and table rows read better unwrapped)
"""Phase 3 on our own record (run on the VPS): build `prices` + `sample_markets` from the
collector's raw trade files, so analyze.py runs unchanged on what we collected.

Reads data/raw/polymarket/trades/**/*.jsonl.gz directly (never the server's DuckDB store, whose
write lock the server holds). For each market, token 0's price on a 10-minute grid = the last
trade in each bucket, using outcome-index-1 trades as 1 - p when that is all a bucket has.
"Volume" is the market's dollars traded in our record (not lifetime), so the liquidity tiers
mean something different here; close = the market's last trade in our record, so a spike needs
72h of our record after it. Writes to data/feasibility/collector.duckdb.

    nice -n 19 ionice -c3 uv run python experiments/feasibility/collector_series.py
    FEASIBILITY_DB=data/feasibility/collector.duckdb uv run python experiments/feasibility/analyze.py --no-census
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
from api import ROOT

RAW = ROOT / "data" / "raw" / "polymarket" / "trades"
OUT_DB = ROOT / "data" / "feasibility" / "collector.duckdb"


def run(raw: Path, out: Path, min_usd: float) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(out))
    con.execute("SET memory_limit = '1GB'; SET threads = 2")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW raw AS
        SELECT DISTINCT ON (transactionHash, asset, side, size, price) *
        FROM read_json('{raw}/*/*/*/*.jsonl.gz', format = 'newline_delimited', compression = 'gzip',
             columns = {{asset: 'VARCHAR', conditionId: 'VARCHAR', size: 'DOUBLE', price: 'DOUBLE',
                        timestamp: 'BIGINT', title: 'VARCHAR', eventSlug: 'VARCHAR',
                        outcomeIndex: 'INTEGER', transactionHash: 'VARCHAR', side: 'VARCHAR'}})
        WHERE conditionId IS NOT NULL AND outcomeIndex IN (0, 1)
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE prices AS
        WITH b AS (
            SELECT conditionId AS condition_id, timestamp // 600 * 600 AS ts,
                   arg_max(CASE WHEN outcomeIndex = 0 THEN price ELSE 1 - price END,
                           (outcomeIndex = 0, timestamp)) AS p
            FROM raw GROUP BY ALL
        )
        SELECT condition_id AS token_id, condition_id, ts, p FROM b
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE sample_markets AS
        SELECT conditionId AS condition_id, coalesce(any_value(eventSlug), conditionId) AS ev,
               any_value(title) AS question, sum(size * price) AS volume,
               strftime(to_timestamp(min(timestamp)), '%Y-%m-%dT%H:%M:%SZ') AS start_date,
               strftime(to_timestamp(max(timestamp)), '%Y-%m-%dT%H:%M:%SZ') AS end_date,
               NULL::VARCHAR AS closed_time, '' AS tags, 'record' AS stratum, 1.0 AS weight,
               NULL::INTEGER AS winner_index, sum(size * price) AS ev_volume
        FROM raw GROUP BY conditionId
        HAVING sum(size * price) >= ? AND any_value(title) NOT ILIKE '%up or down%'
        """,
        [min_usd],
    )
    print(
        con.execute("SELECT count(*), sum(volume)::BIGINT FROM sample_markets").fetchone(),
        con.execute("SELECT count(*) FROM prices").fetchone(),
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, default=RAW)
    ap.add_argument("--out", type=Path, default=OUT_DB)
    ap.add_argument("--min-usd", type=float, default=1000)
    a = ap.parse_args()
    run(a.raw, a.out, a.min_usd)
