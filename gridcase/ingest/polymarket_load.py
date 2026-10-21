"""Load collected Polymarket trades into the store, and give every market a category.

`load_raw` reads the collector's hourly gzip JSONL files (only those that changed since the
last load), keeps each trade once (trade_id = hash of transactionHash, asset, side, size,
price), records collector gaps, less those later filled from the feed's history, and records how
far the collector's record is complete.
`enrich_markets` looks up markets not yet known in Polymarket's public metadata API
(condition id -> event -> tags) and assigns a category from ontology/categories.yaml. Both are
safe to run repeatedly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import duckdb
import httpx
import yaml

from gridcase.config import ONTOLOGY_DIR

log = logging.getLogger("gridcase")

GAMMA_URL = "https://gamma-api.polymarket.com"
BATCH = 50

COLUMNS = (
    "{proxyWallet: 'VARCHAR', side: 'VARCHAR', asset: 'VARCHAR', conditionId: 'VARCHAR', "
    "size: 'DOUBLE', price: 'DOUBLE', timestamp: 'BIGINT', title: 'VARCHAR', slug: 'VARCHAR', "
    "eventSlug: 'VARCHAR', outcome: 'VARCHAR', transactionHash: 'VARCHAR', "
    "collected_at: 'VARCHAR'}"
)


@dataclass
class LoadResult:
    files: int
    new: int
    gaps: int


def _naive_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(UTC).replace(tzinfo=None)


def _load_file(conn: duckdb.DuckDBPyConnection, path: Path) -> int:
    conn.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE staged AS
        SELECT md5(concat_ws('|', transactionHash, asset, side, CAST(size AS VARCHAR),
                             CAST(price AS VARCHAR))) AS trade_id,
               CAST(to_timestamp(timestamp) AS TIMESTAMP) AS ts,
               proxyWallet AS wallet, side, size, price, size * price AS usd, asset,
               conditionId AS condition_id, slug AS market_slug,
               nullif(eventSlug, '') AS event_slug, outcome, title,
               transactionHash AS tx_hash,
               CAST(CAST(collected_at AS TIMESTAMPTZ) AS TIMESTAMP) AS collected_at
        FROM read_json(?, format = 'newline_delimited', compression = 'gzip',
                       columns = {COLUMNS})
        WHERE proxyWallet IS NOT NULL AND conditionId IS NOT NULL
        """,
        [str(path)],
    )
    before = conn.execute("SELECT count(*) FROM trades").fetchone()
    conn.execute(
        """
        INSERT INTO trades
        SELECT trade_id, ts, wallet, side, size, price, usd, asset, condition_id, market_slug,
               event_slug, outcome, title, tx_hash, collected_at
        FROM staged
        QUALIFY row_number() OVER (PARTITION BY trade_id ORDER BY collected_at) = 1
          AND trade_id NOT IN (
              SELECT t.trade_id FROM trades t
              WHERE t.ts BETWEEN (SELECT min(ts) FROM staged) AND (SELECT max(ts) FROM staged))
        """
    )
    after = conn.execute("SELECT count(*) FROM trades").fetchone()
    conn.execute("DROP TABLE staged")
    return int(after[0] if after else 0) - int(before[0] if before else 0)


def _gap_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _span(g: dict[str, Any]) -> tuple[datetime, datetime]:
    lo = datetime.fromtimestamp(int(g["from_ts"]), UTC).replace(tzinfo=None)
    hi = datetime.fromtimestamp(int(g["to_ts"]), UTC).replace(tzinfo=None)
    return lo, hi


def _load_gaps(conn: duckdb.DuckDBPyConnection, path: Path, filled_path: Path) -> int:
    """Record the collector's gaps, except those later filled from the feed's history
    (gaps_filled.jsonl), which are removed if already recorded."""
    filled = {_span(g) for g in _gap_lines(filled_path)}
    for lo, hi in filled:
        conn.execute("DELETE FROM collector_gaps WHERE gap_from = ? AND gap_to = ?", [lo, hi])
    added = 0
    for g in _gap_lines(path):
        lo, hi = _span(g)
        if (lo, hi) in filled:
            continue
        exists = conn.execute(
            "SELECT count(*) FROM collector_gaps WHERE gap_from = ? AND gap_to = ?", [lo, hi]
        ).fetchone()
        if exists and exists[0]:
            continue
        detected = _naive_utc(g["detected_at"]) if g.get("detected_at") else None
        conn.execute("INSERT INTO collector_gaps VALUES (?, ?, ?)", [lo, hi, detected])
        added += 1
    return added


def _complete_to(root: Path) -> datetime | None:
    path = root / "collector_state.json"
    if not path.exists():
        return None
    ts = json.loads(path.read_text()).get("complete_to")
    return None if ts is None else datetime.fromtimestamp(int(ts), UTC).replace(tzinfo=None)


def load_raw(
    conn: duckdb.DuckDBPyConnection, root: Path, now: datetime | None = None
) -> LoadResult:
    # read the collector's mark before its files: every trade up to the mark is then loaded
    mark = _complete_to(root)
    files = new = 0
    for path in sorted((root / "trades").rglob("*.jsonl.gz")):
        st = path.stat()
        known = conn.execute(
            "SELECT size, mtime FROM raw_files WHERE path = ?", [str(path)]
        ).fetchone()
        if known and known[0] == st.st_size and known[1] == st.st_mtime:
            continue
        new += _load_file(conn, path)
        conn.execute(
            "INSERT OR REPLACE INTO raw_files VALUES (?, ?, ?)",
            [str(path), st.st_size, st.st_mtime],
        )
        files += 1
    gaps = _load_gaps(conn, root / "gaps.jsonl", root / "gaps_filled.jsonl")
    last = conn.execute(
        "SELECT complete_to FROM collector_marks ORDER BY loaded_at DESC LIMIT 1"
    ).fetchone()
    if mark is not None and (last is None or last[0] != mark):
        loaded_at = now or datetime.now(UTC).replace(tzinfo=None)
        conn.execute("INSERT INTO collector_marks VALUES (?, ?)", [mark, loaded_at])
    return LoadResult(files, new, gaps)


# ------------------------------------------------------------------ categories


@cache
def _rules() -> tuple[list[dict[str, Any]], str]:
    doc = yaml.safe_load((ONTOLOGY_DIR / "categories.yaml").read_text())
    return doc["categories"], doc["default"]


def categorize(tags: list[str], title: str) -> str:
    rules, default = _rules()
    lowered = {t.strip().lower() for t in tags}
    for r in rules:
        if lowered & {t.lower() for t in r.get("tags", [])}:
            return str(r["name"])
    text = f" {title.lower()} "
    for r in rules:
        if any(k.lower() in text for k in r.get("keywords", [])):
            return str(r["name"])
    return default


def enrich_markets(conn: duckdb.DuckDBPyConnection, client: httpx.Client) -> int:
    """Categorise markets seen in trades but not yet in `markets`. Returns markets added."""
    todo = conn.execute(
        """
        SELECT t.condition_id, any_value(t.event_slug), any_value(t.title)
        FROM trades t LEFT JOIN markets m USING (condition_id)
        WHERE m.condition_id IS NULL GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    added = 0
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        found: dict[str, Any] = {}
        try:
            for closed in ("false", "true"):  # the API returns open markets unless asked
                missing = [c for c, _, _ in batch if c not in found]
                if not missing:
                    break
                resp = client.get(
                    "/markets",
                    params=[("condition_ids", c) for c in missing] + [("closed", closed)],
                    timeout=30,
                )
                resp.raise_for_status()
                found |= {m.get("conditionId"): m for m in resp.json()}
        except (httpx.HTTPError, ValueError) as err:
            log.warning("market lookup failed: %s", err)
            continue
        event_of: dict[str, str | None] = {}
        for cid, trade_event, _ in batch:
            events = (found.get(cid) or {}).get("events") or []
            event_of[cid] = (events[0].get("slug") if events else None) or trade_event
        slugs = sorted({s for s in event_of.values() if s})
        tags_of: dict[str, tuple[str, list[str]]] = {}
        try:
            if slugs:
                ev = client.get("/events", params=[("slug", s) for s in slugs], timeout=30)
                ev.raise_for_status()
                for event in ev.json():
                    tags_of[event.get("slug")] = (
                        event.get("title") or "",
                        [t.get("label", "") for t in event.get("tags") or []],
                    )
        except (httpx.HTTPError, ValueError) as err:
            log.warning("event lookup failed: %s", err)
            continue
        for cid, _, trade_title in batch:
            m = found.get(cid) or {}
            slug = event_of[cid]
            event_title, tags = tags_of.get(slug or "", ("", []))
            question = m.get("question") or trade_title or ""
            category = categorize(tags, f"{event_title} {question}")
            conn.execute(
                "INSERT OR REPLACE INTO markets VALUES (?, ?, ?, ?, ?, ?, ?)",
                [cid, question, slug, event_title, category, "|".join(tags), now],
            )
            added += 1
    return added


@dataclass
class RefreshResult:
    files: int
    new: int
    gaps: int
    markets_added: int


def refresh(conn: duckdb.DuckDBPyConnection, root: Path, client: httpx.Client) -> RefreshResult:
    """What the server runs every few minutes: load new trades, then categorise new markets."""
    loaded = load_raw(conn, root)
    return RefreshResult(loaded.files, loaded.new, loaded.gaps, enrich_markets(conn, client))
