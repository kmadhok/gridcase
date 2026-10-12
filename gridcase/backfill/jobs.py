"""Backfill jobs (the `gridcase backfill ...` commands): each writes Parquet under
data/backfill/ and can be rerun; the price and trade jobs skip markets already written and the
client's cache makes an interrupted chunk free to redo.

Layout:
  markets.parquet       census rows (winner fields included; build.py splits them off)
  resolutions.parquet   /v2/resolutions, one row per market
  prices/part-*.parquet token 0 price series (condition_id, ts, p)
  trades/part-*.parquet taker trades for task markets
  trades_status.parquet per task market: whether its walk reached the needed date
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import duckdb

from gridcase.backfill.census import fetch_census
from gridcase.backfill.client import Client
from gridcase.backfill.fetch import clob_prices, life, settled_token0, v2_resolutions, v2_trades

log = logging.getLogger("gridcase")

MARKET_COLUMNS = {
    "condition_id": "VARCHAR", "market_id": "VARCHAR", "question": "VARCHAR",
    "slug": "VARCHAR", "event_id": "VARCHAR", "event_slug": "VARCHAR",
    "event_title": "VARCHAR", "tags": "VARCHAR", "category": "VARCHAR", "volume": "DOUBLE",
    "start_ts": "BIGINT", "end_ts": "BIGINT", "closed_ts": "BIGINT", "neg_risk": "BOOLEAN",
    "token0": "VARCHAR", "token1": "VARCHAR", "outcome0": "VARCHAR", "outcome1": "VARCHAR",
    "winner_index": "INTEGER", "uma_status": "VARCHAR",
}  # fmt: skip
RESOLUTION_COLUMNS = {
    "condition_id": "VARCHAR", "status": "VARCHAR", "settled_token0": "DOUBLE",
    "was_disputed": "BOOLEAN", "resolved_at": "VARCHAR",
}  # fmt: skip
PRICE_COLUMNS = {"condition_id": "VARCHAR", "ts": "BIGINT", "p": "DOUBLE"}
TRADE_COLUMNS = {
    "condition_id": "VARCHAR", "token_id": "VARCHAR", "proxy_wallet": "VARCHAR",
    "side": "VARCHAR", "size": "DOUBLE", "price": "DOUBLE", "timestamp": "BIGINT",
    "outcome": "VARCHAR", "outcome_index": "INTEGER", "transaction_hash": "VARCHAR",
    "title": "VARCHAR", "slug": "VARCHAR", "event_slug": "VARCHAR",
}  # fmt: skip
STATUS_COLUMNS = {"condition_id": "VARCHAR", "needed_from": "BIGINT", "complete": "BOOLEAN"}


def write_parquet(rows: Iterable[dict[str, Any]], columns: dict[str, str], path: Path) -> int:
    """Rows -> Parquet with an explicit schema (through a temporary JSONL file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for r in rows:
            f.write(json.dumps({k: r.get(k) for k in columns}) + "\n")
            n += 1
    src = Path(f.name)
    try:
        spec = "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"
        tmp = path.with_suffix(".tmp.parquet")
        con = duckdb.connect()
        con.execute(
            f"COPY (SELECT * FROM read_json(?, format = 'newline_delimited', columns = {spec})) "
            f"TO '{tmp}' (FORMAT parquet)",
            [str(src)],
        )
        con.close()
        tmp.replace(path)
    finally:
        src.unlink()
    return n


def read_parquet(path: Path | str, sql: str = "SELECT * FROM t") -> list[tuple[Any, ...]]:
    con = duckdb.connect()
    con.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{path}', union_by_name = true)")
    rows = con.execute(sql).fetchall()
    con.close()
    return rows


def run_markets(
    client: Client, out_dir: Path, start: str, end: str, min_volume: float
) -> dict[str, Any]:
    rows, pages, complete = fetch_census(client, start, end, min_volume)
    n = write_parquet(rows, MARKET_COLUMNS, out_dir / "markets.parquet")
    return {"markets": n, "pages": pages, "complete": complete}


def run_resolutions(client: Client, out_dir: Path, workers: int = 4) -> dict[str, Any]:
    ids = [r[0] for r in read_parquet(out_dir / "markets.parquet", "SELECT condition_id FROM t")]
    batches = [ids[i : i + 20] for i in range(0, len(ids), 20)]
    found: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(workers) as pool:
        for part in pool.map(lambda b: v2_resolutions(client, b), batches):
            found |= part
    rows = [
        {
            "condition_id": cid,
            "status": r.get("status"),
            "settled_token0": settled_token0(r),
            "was_disputed": r.get("was_disputed"),
            "resolved_at": str(r.get("resolved_at") or r.get("last_update_timestamp") or ""),
        }
        for cid, r in found.items()
    ]
    n = write_parquet(rows, RESOLUTION_COLUMNS, out_dir / "resolutions.parquet")
    return {"markets": len(ids), "resolutions": n}


def _done(dir_: Path) -> set[str]:
    if not dir_.exists() or not any(dir_.glob("part-*.parquet")):
        return set()
    return {
        r[0] for r in read_parquet(dir_ / "part-*.parquet", "SELECT DISTINCT condition_id FROM t")
    }


def _run_parts(
    items: list[Any],
    work: Callable[[Any], list[dict[str, Any]]],
    dir_: Path,
    columns: dict[str, str],
    batch: int,
    workers: int,
    progress: Callable[[str], None],
) -> int:
    start = len(list(dir_.glob("part-*.parquet"))) if dir_.exists() else 0
    total = 0
    for b, i in enumerate(range(0, len(items), batch)):
        chunk = items[i : i + batch]
        with ThreadPoolExecutor(workers) as pool:
            rows = [r for part in pool.map(work, chunk) for r in part]
        write_parquet(rows, columns, dir_ / f"part-{start + b:05d}.parquet")
        total += len(rows)
        progress(f"{min(i + batch, len(items))}/{len(items)} markets, {total:,} rows")
    return total


def run_prices(
    client: Client,
    out_dir: Path,
    fidelity_min: int = 10,
    workers: int = 8,
    batch: int = 400,
    limit: int | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    rows = read_parquet(
        out_dir / "markets.parquet",
        "SELECT condition_id, token0, start_ts, end_ts, closed_ts FROM t ORDER BY volume DESC",
    )
    done = _done(out_dir / "prices")
    todo = [r for r in rows if r[0] not in done][: limit or None]

    def work(r: tuple[Any, ...]) -> list[dict[str, Any]]:
        span = life(r[2], r[3], r[4])
        if span is None:
            return [{"condition_id": r[0], "ts": None, "p": None}]  # marks it done, no points
        pts = clob_prices(client, r[1], span[0], span[1], fidelity_min)
        return [{"condition_id": r[0], "ts": t, "p": p} for t, p in pts] or [
            {"condition_id": r[0], "ts": None, "p": None}
        ]

    n = _run_parts(todo, work, out_dir / "prices", PRICE_COLUMNS, batch, workers, progress)
    return {"markets": len(rows), "already_done": len(done), "fetched": len(todo), "rows": n}


def run_trades(
    client: Client,
    out_dir: Path,
    needed: dict[str, int],
    max_pages: int = 500,
    workers: int = 4,
    batch: int = 100,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Trades for the given markets back to `needed[condition_id]` (epoch s)."""
    done = _done(out_dir / "trades")
    todo = sorted(c for c in needed if c not in done)
    status: list[dict[str, Any]] = []

    def work(cid: str) -> list[dict[str, Any]]:
        rows, complete = v2_trades(client, cid, needed[cid], max_pages)
        status.append({"condition_id": cid, "needed_from": needed[cid], "complete": complete})
        return [{**r, "condition_id": cid} for r in rows] or [{"condition_id": cid}]

    n = _run_parts(todo, work, out_dir / "trades", TRADE_COLUMNS, batch, workers, progress)
    old: list[dict[str, Any]] = []
    status_path = out_dir / "trades_status.parquet"
    if status_path.exists():
        old = [dict(zip(STATUS_COLUMNS, r, strict=True)) for r in read_parquet(status_path)]
    write_parquet(old + status, STATUS_COLUMNS, status_path)
    return {
        "markets": len(needed),
        "fetched": len(todo),
        "rows": n,
        "incomplete": sum(1 for s in status if not s["complete"]),
    }
