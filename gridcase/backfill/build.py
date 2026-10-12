"""Build data/backfill.duckdb from the Parquet files in data/backfill/.

Written only by `gridcase backfill build`; tools open it read-only. Tables:
  hist_markets     what tools may see about a market (no outcome fields)
  hist_outcomes    the grader's answers: Gamma winner and /v2/resolutions settled price
  hist_prices      token 0 price series (condition_id, ts, p)
  trades, markets  the live store's own schema (gridcase/store/schema.py) filled with backfilled
                   taker trades, collected_at = ts, so trades_as_of, the semantic layer and the
                   kpi.* / by.* tools run unchanged. "New trader" means new to this history.
  trades_status    per task market, whether the trade walk reached the date its tasks need
Macros prices_as_of(t) and hist_markets_as_of(t) give the point-in-time views tools read.
"""

from __future__ import annotations

from pathlib import Path

from gridcase.store.db import connect

HIST_DDL = [
    "CREATE OR REPLACE MACRO prices_as_of(t) AS TABLE SELECT * FROM hist_prices WHERE ts <= t",
    "CREATE OR REPLACE MACRO hist_markets_as_of(t) AS TABLE "
    "SELECT * FROM hist_markets WHERE start_at <= t",
]


def build(src: Path, db_path: Path) -> dict[str, int]:
    if db_path.exists():
        db_path.unlink()
    wal = db_path.with_suffix(db_path.suffix + ".wal")
    if wal.exists():
        wal.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(db_path)  # creates the live-store schema (trades, markets, trades_as_of, ...)
    con.execute("SET threads = 4")  # a bulk build; receipts are produced later, single-threaded
    m = f"read_parquet('{src / 'markets.parquet'}')"
    con.execute(
        f"""
        CREATE TABLE hist_markets AS
        SELECT condition_id, market_id, question, slug, event_id, event_slug, event_title, tags,
               category, volume, neg_risk, token0, token1, outcome0, outcome1,
               CAST(to_timestamp(start_ts) AS TIMESTAMP) AS start_at,
               CAST(to_timestamp(end_ts) AS TIMESTAMP) AS end_at,
               CAST(to_timestamp(closed_ts) AS TIMESTAMP) AS closed_at
        FROM {m} WHERE start_ts IS NOT NULL
        """
    )
    res = src / "resolutions.parquet"
    res_sql = (
        f"LEFT JOIN read_parquet('{res}') r USING (condition_id)"
        if res.exists()
        else "LEFT JOIN (SELECT NULL::VARCHAR AS condition_id, NULL::DOUBLE AS settled_token0, "
        "NULL::VARCHAR AS status, NULL::BOOLEAN AS was_disputed) r USING (condition_id)"
    )
    con.execute(
        f"""
        CREATE TABLE hist_outcomes AS
        SELECT condition_id, winner_index, uma_status, r.status AS v2_status,
               r.settled_token0, r.was_disputed,
               CASE WHEN winner_index = 0 THEN 1.0 ELSE 0.0 END AS y,
               r.settled_token0 IS NULL OR r.settled_token0 = (winner_index = 0)::INT AS agrees
        FROM {m} {res_sql}
        """
    )
    prices = src / "prices"
    if any(prices.glob("part-*.parquet")):
        con.execute(
            f"""
            CREATE TABLE hist_prices AS
            SELECT condition_id, CAST(to_timestamp(ts) AS TIMESTAMP) AS ts, p
            FROM read_parquet('{prices / "part-*.parquet"}')
            WHERE ts IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY condition_id, ts) = 1
            ORDER BY condition_id, ts
            """
        )
    else:
        con.execute("CREATE TABLE hist_prices (condition_id VARCHAR, ts TIMESTAMP, p DOUBLE)")
    trades = src / "trades"
    if any(trades.glob("part-*.parquet")):
        con.execute(
            f"""
            INSERT INTO trades
            SELECT md5(concat_ws('|', transaction_hash, token_id, side, CAST(size AS VARCHAR),
                                 CAST(price AS VARCHAR))) AS trade_id,
                   CAST(to_timestamp(timestamp) AS TIMESTAMP) AS ts, proxy_wallet, side, size,
                   price, size * price, token_id, condition_id, slug, nullif(event_slug, ''),
                   outcome, title, transaction_hash, CAST(to_timestamp(timestamp) AS TIMESTAMP)
            FROM read_parquet('{trades / "part-*.parquet"}')
            WHERE timestamp IS NOT NULL AND proxy_wallet IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY trade_id) = 1
            """
        )
        con.execute(
            """
            INSERT INTO markets
            SELECT condition_id, question, event_slug, event_title, category, tags,
                   now()::TIMESTAMP
            FROM hist_markets WHERE condition_id IN (SELECT DISTINCT condition_id FROM trades)
            """
        )
    status = [p for p in (src / "trades_status.parquet", src / "trades_status") if p.exists()]
    globs = [str(p) if p.is_file() else str(p / "part-*.parquet") for p in status]
    if globs:
        con.execute(
            "CREATE TABLE trades_status AS SELECT condition_id, any_value(needed_from) AS "
            "needed_from, bool_and(complete) AS complete FROM read_parquet(?) GROUP BY 1",
            [globs],
        )
    else:
        con.execute(
            "CREATE TABLE trades_status "
            "(condition_id VARCHAR, needed_from BIGINT, complete BOOLEAN)"
        )
    for stmt in HIST_DDL:
        con.execute(stmt)
    counts = {
        t: int((con.execute(f"SELECT count(*) FROM {t}").fetchone() or [0])[0])
        for t in ("hist_markets", "hist_outcomes", "hist_prices", "trades", "markets")
    }
    con.close()
    return counts
