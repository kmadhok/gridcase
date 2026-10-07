"""Polymarket trades collector.

The public trades feed (data-api.polymarket.com/trades) is newest-first and stops at an offset
of 10,000 (about 20 minutes of trading), with no date filter: history exists only if it is
saved as it happens. `poll_once` fetches the newest page, keeps trades not seen before, and
appends them to an hourly gzip JSONL file under <root>/trades/YYYY/MM/DD/HH.jsonl.gz (the raw,
append-only record the store is built from). A gap is reported when a page no longer reaches
back to the newest trade already saved.
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

log = logging.getLogger("gridcase")

BASE_URL = "https://data-api.polymarket.com"
# The feed is cached at the CDN for 5 minutes per URL. The largest page (10,000 trades) spans
# ~11 minutes at current rates, so polling every 4 minutes keeps consecutive pages overlapping
# without cache-busting; the gap check reports it if trading ever outruns that.
PAGE = 10_000
COLLECT_EVERY_S = 240.0  # the 4-minute poll above
KEEP_SEEN_S = 1800  # remember trade identities this far behind the newest one


def trade_key(t: dict[str, Any]) -> tuple[Any, ...]:
    # one transaction can fill several outcomes/orders, so the hash alone is not an identity
    return (t.get("transactionHash"), t.get("asset"), t.get("side"), t.get("size"), t.get("price"))


@dataclass
class CollectorState:
    last_ts: int | None = None
    seen: dict[tuple[Any, ...], int] = field(default_factory=dict)


@dataclass
class PollResult:
    new: int
    gap: bool = False
    error: str | None = None


def _hour_file(root: Path, ts: int) -> Path:
    d = datetime.fromtimestamp(ts, UTC)
    return root / "trades" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / f"{d:%H}.jsonl.gz"


def poll_once(
    client: httpx.Client, root: Path, state: CollectorState, limit: int = PAGE
) -> PollResult:
    try:
        resp = client.get("/trades", params={"limit": limit}, timeout=30)
        resp.raise_for_status()
        page: list[dict[str, Any]] = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return PollResult(0, error=str(e))
    if not page:
        return PollResult(0)
    oldest = min(int(t["timestamp"]) for t in page)
    gap = state.last_ts is not None and oldest > state.last_ts
    if gap:  # trades between the last saved one and this page's oldest are lost for good
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
    return PollResult(len(fresh), gap=gap)


def run_collector(root: Path, interval_s: float = COLLECT_EVERY_S) -> None:
    """Poll forever. Logs a line every 15 polls and every gap or error."""
    state = CollectorState()
    client = httpx.Client(base_url=BASE_URL, headers={"User-Agent": "gridcase"})
    polls = saved = 0
    while True:
        r = poll_once(client, root, state)
        polls += 1
        saved += r.new
        if r.error:
            log.warning("polymarket poll failed: %s", r.error)
        if r.gap:
            log.warning("polymarket gap: the newest page no longer reached the last saved trade")
        if polls % 3 == 0:
            log.info("polymarket: %d trades saved in the last 3 polls", saved)
            saved = 0
        time.sleep(interval_s)
