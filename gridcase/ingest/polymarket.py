"""Polymarket trades collector.

`poll_once` fetches the newest trades, keeps those not seen before, and appends them to an
hourly gzip JSONL file under <root>/trades/YYYY/MM/DD/HH.jsonl.gz (the raw, append-only record
the store is built from). A gap is reported when a poll cannot reach back to the newest trade
already saved.

Two feeds, chosen by GRIDCASE_TRADES_API (config.TRADES_API):
- v2 (default): data-api.polymarket.com/v2/trades, newest first with a keyset cursor, 1,000
  trades a page, reaching back about a month. Each poll walks pages until one reaches the last
  saved trade, at most MAX_PAGES pages. Rows are renamed to v1's camelCase keys
  (`v2_to_v1`) so the raw file format, the loader and dbt are unchanged.
- v1: data-api.polymarket.com/trades, one page of the newest 10,000 trades (no deeper paging).
  Polymarket retires it on 2026-10-24; kept so a deploy can flip back if v2 counts look wrong.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from gridcase import config

log = logging.getLogger("gridcase")

BASE_URL = "https://data-api.polymarket.com"
# v1 is cached at the CDN for 5 minutes per URL. Its largest page (10,000 trades) spans ~11
# minutes at current rates, so polling every 4 minutes keeps consecutive pages overlapping
# without cache-busting; the gap check reports it if trading ever outruns that. v2 keeps the
# same 4-minute poll: ~4 pages of 1,000 at current rates.
PAGE = 10_000
V2_PAGE = 1_000  # v2's largest page
# v2 pages per poll: 20,000 trades (~20 minutes of trading), far below the published 300
# requests per 10 s, and enough to cover one failed poll.
MAX_PAGES = 20
COLLECT_EVERY_S = 240.0  # the 4-minute poll above
KEEP_SEEN_S = 1800  # remember trade identities this far behind the newest one

# v2 key -> v1 key; every other key (side, size, price, timestamp, title, slug, icon, outcome,
# name, pseudonym, bio) has the same name in both. Checked live on 2026-10-15: 535 trades seen
# in both feeds were identical after this renaming except for `icon` URLs on 50 of them.
V2_TO_V1 = {
    "proxy_wallet": "proxyWallet",
    "token_id": "asset",
    "condition_id": "conditionId",
    "event_slug": "eventSlug",
    "outcome_index": "outcomeIndex",
    "profile_image": "profileImage",
    "profile_image_optimized": "profileImageOptimized",
    "transaction_hash": "transactionHash",
}


def v2_to_v1(row: dict[str, Any]) -> dict[str, Any]:
    """One /v2/trades row in the v1 raw shape (keys renamed, values untouched)."""
    return {V2_TO_V1.get(k, k): v for k, v in row.items()}


def trade_key(t: dict[str, Any]) -> tuple[Any, ...]:
    # one transaction can fill several outcomes/orders, so the hash alone is not an identity
    return (t.get("transactionHash"), t.get("asset"), t.get("side"), t.get("size"), t.get("price"))


def trades_url(api: str) -> str:
    return f"{BASE_URL}/v2/trades" if api == "v2" else f"{BASE_URL}/trades"


@dataclass
class CollectorState:
    last_ts: int | None = None
    seen: dict[tuple[Any, ...], int] = field(default_factory=dict)


@dataclass
class PollResult:
    new: int
    gap: bool = False
    error: str | None = None
    pages: int = 1


def _hour_file(root: Path, ts: int) -> Path:
    d = datetime.fromtimestamp(ts, UTC)
    return root / "trades" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / f"{d:%H}.jsonl.gz"


def _fetch_v1(client: httpx.Client, limit: int) -> tuple[list[dict[str, Any]], bool, int]:
    resp = client.get("/trades", params={"limit": limit}, timeout=30)
    resp.raise_for_status()
    page: list[dict[str, Any]] = resp.json()
    return page, False, 1


def _fetch_v2(
    client: httpx.Client, last_ts: int | None, max_pages: int
) -> tuple[list[dict[str, Any]], bool, int]:
    """Walk /v2/trades newest first until a page goes older than `last_ts` (so every trade
    in last_ts's own second is covered), the feed ends, or `max_pages` run out. Returns
    (rows in v1 shape, cut short by the cap, pages fetched). Without `last_ts` (first poll)
    one page is enough. taker_only=true is v2's default and what v1 served; it is pinned so
    a change of default cannot double-count both sides of each fill."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for n in range(1, max_pages + 1):
        params: dict[str, Any] = {"limit": V2_PAGE, "taker_only": "true"}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/v2/trades", params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("unexpected /v2/trades response")
        page = [v2_to_v1(r) for r in body.get("data") or []]
        rows += page
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if not page or not cursor or last_ts is None:
            return rows, False, n
        if min(int(t["timestamp"]) for t in page) < last_ts:
            return rows, False, n
    return rows, True, max_pages


def poll_once(
    client: httpx.Client,
    root: Path,
    state: CollectorState,
    limit: int = PAGE,
    api: str | None = None,
    max_pages: int = MAX_PAGES,
) -> PollResult:
    """One poll of the trades feed (`api` "v1" or "v2", default config.TRADES_API). A failed
    request saves nothing, so the next poll retries from the same last saved trade."""
    api = api or config.TRADES_API
    try:
        if api == "v2":
            page, capped, pages = _fetch_v2(client, state.last_ts, max_pages)
        else:
            page, capped, pages = _fetch_v1(client, limit)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        return PollResult(0, error=str(e))
    if not page:
        return PollResult(0, pages=pages)
    oldest = min(int(t["timestamp"]) for t in page)
    # v1: the single page no longer reaches the last saved trade. v2: the cursor walk hit the
    # page cap before reaching it (if the feed simply ended, there is nothing older to miss).
    gap = state.last_ts is not None and oldest > state.last_ts and (api != "v2" or capped)
    if gap:  # trades between the last saved one and this poll's oldest are lost for good
        root.mkdir(parents=True, exist_ok=True)
        with (root / "gaps.jsonl").open("a") as f:
            detected = datetime.now(UTC).isoformat(timespec="seconds")
            f.write(
                json.dumps({"from_ts": state.last_ts, "to_ts": oldest, "detected_at": detected})
                + "\n"
            )
    fresh = [t for t in page if trade_key(t) not in state.seen]
    fresh.sort(key=lambda t: int(t["timestamp"]))
    now = datetime.now(UTC).isoformat(timespec="seconds")
    by_file: dict[Path, list[str]] = {}
    for t in fresh:
        state.seen[trade_key(t)] = int(t["timestamp"])
        line = json.dumps({**t, "collected_at": now}, separators=(",", ":"))
        by_file.setdefault(_hour_file(root, int(t["timestamp"])), []).append(line)
    for path, lines in by_file.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "at") as f:
            f.write("\n".join(lines) + "\n")
    newest = max(int(t["timestamp"]) for t in page)
    state.last_ts = max(newest, state.last_ts or newest)
    cutoff = state.last_ts - KEEP_SEEN_S
    state.seen = {k: ts for k, ts in state.seen.items() if ts >= cutoff}
    return PollResult(len(fresh), gap=gap, pages=pages)


def run_collector(root: Path, interval_s: float = COLLECT_EVERY_S) -> None:
    """Poll forever. Logs a line every 3 polls and every gap or error."""
    api = config.TRADES_API
    if api not in ("v1", "v2"):
        raise SystemExit(f"GRIDCASE_TRADES_API must be v1 or v2, not {api!r}")
    log.info("polymarket: collecting from %s", trades_url(api))
    state = CollectorState()
    client = httpx.Client(base_url=BASE_URL, headers={"User-Agent": "gridcase"})
    polls = saved = 0
    while True:
        r = poll_once(client, root, state, api=api)
        polls += 1
        saved += r.new
        if r.error:
            log.warning("polymarket poll failed: %s", r.error)
        if r.gap:
            log.warning("polymarket gap: the poll no longer reached the last saved trade")
        if polls % 3 == 0:
            log.info("polymarket: %d trades saved in the last 3 polls", saved)
            saved = 0
        time.sleep(interval_s)
