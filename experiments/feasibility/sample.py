# ruff: noqa: E501  (SQL and table rows read better unwrapped)
"""Phase 3 sample: 10-minute price series for a stratified random sample of whole events.

Events (not markets) are sampled, so that sibling markets reacting to the same news are all
present and event clustering can be measured. Frame: `census` markets that are resolved binary
(winner known), not recurring "Up or Down" series, volume >= $1k. Events are stratified by
total volume; within a sampled event every qualifying market is fetched (largest first, at most
--max-markets-per-event). For each market: token 0's CLOB series at 10-minute fidelity over its
whole life. Writes `sample_markets` (with stratum weights) and `prices` to backfill.duckdb.

    uv run python experiments/feasibility/sample.py --per-stratum 40 --seed 7
"""

from __future__ import annotations

import argparse
from typing import Any

import duckdb
from api import Api
from backfill import clob_prices
from census import DB
from probe import life

STRATA = (
    "CASE WHEN ev_volume < 1e4 THEN 'a <10k' WHEN ev_volume < 1e5 THEN 'b 10k-100k' "
    "WHEN ev_volume < 1e6 THEN 'c 100k-1M' WHEN ev_volume < 1e7 THEN 'd 1M-10M' ELSE 'e 10M+' END"
)

FRAME = """
CREATE OR REPLACE TABLE frame AS
WITH m AS (
    SELECT *, coalesce(event_id, condition_id) AS ev FROM census
    WHERE NOT recurring AND winner_index IS NOT NULL AND token0 IS NOT NULL
      AND start_date IS NOT NULL AND volume >= 1000
)
SELECT m.*, sum(volume) OVER (PARTITION BY ev) AS ev_volume,
       count(*) OVER (PARTITION BY ev) AS ev_markets
FROM m
"""


def run(per_stratum: int, seed: int, max_markets: int, fidelity: int) -> None:
    con = duckdb.connect(str(DB))
    con.execute(FRAME)
    con.execute(f"CREATE OR REPLACE TABLE frame AS SELECT *, {STRATA} AS stratum FROM frame")
    con.execute(f"SELECT setseed({(seed % 1000) / 1000})")
    con.execute(
        """
        CREATE OR REPLACE TABLE sample_events AS
        WITH ev AS (SELECT ev, any_value(stratum) AS stratum, any_value(ev_volume) AS ev_volume,
                           any_value(ev_markets) AS ev_markets FROM frame GROUP BY ev ORDER BY ev),
             ranked AS (SELECT *, row_number() OVER (PARTITION BY stratum ORDER BY random()) AS rk,
                               count(*) OVER (PARTITION BY stratum) AS stratum_events FROM ev)
        SELECT *, stratum_events / least(stratum_events, ?) AS weight FROM ranked WHERE rk <= ?
        """,
        [per_stratum, per_stratum],
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE sample_markets AS
        SELECT f.*, e.weight,
               row_number() OVER (PARTITION BY f.ev ORDER BY f.volume DESC) AS rank_in_event
        FROM frame f JOIN sample_events e USING (ev)
        QUALIFY rank_in_event <= ?
        """,
        [max_markets],
    )
    todo: list[tuple[Any, ...]] = con.execute(
        "SELECT condition_id, token0, start_date, end_date, closed_time FROM sample_markets"
    ).fetchall()
    api = Api()
    rows: list[tuple[Any, ...]] = []
    for i, (cid, tok, sd, ed, ct) in enumerate(todo):
        lo, hi = life(sd, ed, ct)
        rows += [(tok, cid, t, p) for t, p in clob_prices(api, tok, lo, hi, fidelity)]
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(todo)} markets, {len(rows):,} points", api.stats(), flush=True)
    con.execute(
        "CREATE OR REPLACE TABLE prices (token_id VARCHAR, condition_id VARCHAR, ts BIGINT, p DOUBLE)"
    )
    con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?)", rows)
    print(
        con.execute(
            "SELECT stratum, count(DISTINCT ev) events, count(*) markets, "
            "any_value(weight) weight FROM sample_markets GROUP BY 1 ORDER BY 1"
        ).fetchall()
    )
    print(f"{len(rows):,} price points", api.stats())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-stratum", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--max-markets-per-event", type=int, default=25)
    ap.add_argument("--fidelity", type=int, default=10)
    a = ap.parse_args()
    run(a.per_stratum, a.seed, a.max_markets_per_event, a.fidelity)
