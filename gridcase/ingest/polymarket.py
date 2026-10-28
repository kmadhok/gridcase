"""Polymarket trades collector.

The public trades feed (data-api.polymarket.com/v2/trades) is newest-first, 1,000 trades a page,
with a cursor that walks back about a month and no date filter. `poll_once` pages back from the
newest trade until it reaches the newest trade already saved, and appends the trades not saved
before to an hourly gzip JSONL file under <root>/trades/YYYY/MM/DD/HH.jsonl.gz (the raw,
append-only record the store is built from). Each line keeps the field names of the retired v1
feed, so the raw files, the loader and the warehouse read it unchanged.

How far the record is complete is kept in <root>/collector_state.json and moves only once a walk
reaches it, so a collector that was stopped (a reboot, an outage) fills the time it missed when
it starts again. A gap is reported only when the feed no longer reaches back that far, or a walk
hits its page limit.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("gridcase")

BASE_URL = "https://data-api.polymarket.com"
TRADES_PATH = "/v2/trades"
PAGE = 1_000  # the most v2 serves per page
# About 55 pages cover an hour of trading, so a 4-minute poll is a few pages; the limit lets one
# walk fill roughly three days of downtime (~1 page a second) before the rest is written as a gap.
COLLECT_EVERY_S = 240.0
MAX_PAGES = 4_000
KEEP_SEEN_S = 1800  # remember trade identities this far behind the newest one
RETRIES = 3  # per page, for 429, 5xx and network errors; the cursor is kept
STATE_FILE = "collector_state.json"
FILLED_FILE = (
    "gaps_filled.jsonl"  # gaps later filled from the feed's history; the loader drops them
)
FILL_MAX_PAGES = 45_000  # about the month the feed reaches back
CORRUPT_DIR = "corrupt"  # originals of hour files repaired after a crash
REPAIR_DAYS = 4.0  # past MAX_PAGES' three days of catch-up, so every file a crash touched
GZIP_MAGIC = b"\x1f\x8b\x08"

# v2 answers in snake_case; the raw files keep v1's names
V1_NAMES = {
    "proxy_wallet": "proxyWallet",
    "token_id": "asset",
    "condition_id": "conditionId",
    "event_slug": "eventSlug",
    "outcome_index": "outcomeIndex",
    "profile_image": "profileImage",
    "profile_image_optimized": "profileImageOptimized",
    "transaction_hash": "transactionHash",
}


def trade_key(t: dict[str, Any]) -> tuple[Any, ...]:
    # one transaction can fill several outcomes/orders, so the hash alone is not an identity
    return (t.get("transactionHash"), t.get("asset"), t.get("side"), t.get("size"), t.get("price"))


@dataclass
class CollectorState:
    last_ts: int | None = None  # every trade up to this second is saved (or recorded as a gap)
    seen: dict[tuple[Any, ...], int] = field(default_factory=dict)


@dataclass
class PollResult:
    new: int
    gap: bool = False
    error: str | None = None
    pages: int = 0


class FeedError(Exception):
    pass


def _hour_file(root: Path, ts: int) -> Path:
    d = datetime.fromtimestamp(ts, UTC)
    return root / "trades" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / f"{d:%H}.jsonl.gz"


def _read_lines(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f if line.strip()]


def _complete_lines(data: bytes) -> tuple[list[bytes], bool]:
    """Every whole JSON line in a file of appended gzip members, and whether anything had to be
    left out: bytes that are not a member (zeros left by a crash) or a member cut off mid-write,
    whose last partial line is dropped. Each member's lines are whole (the collector writes one
    member per append), so only a cut-off member can end mid-line."""
    lines: list[bytes] = []
    damaged = False
    pos = 0
    while pos < len(data):
        if data[pos : pos + 3] != GZIP_MAGIC:
            damaged = True
            nxt = data.find(GZIP_MAGIC, pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out = d.decompress(data[pos:])
        except zlib.error:
            out = b""
        if not d.eof:  # cut off: whatever follows its last newline is a partial line
            out = out[: out.rfind(b"\n") + 1]
        lines += [raw for raw in out.split(b"\n") if raw.strip()]
        if d.eof:
            pos = len(data) - len(d.unused_data)
        else:
            damaged = True
            nxt = data.find(GZIP_MAGIC, pos + 1)
            if nxt < 0:
                break
            pos = nxt
    return lines, damaged


def repair_hour_file(path: Path, corrupt_dir: Path) -> bool:
    """Rewrite a damaged hour file with its whole lines, keeping the original in corrupt_dir.
    Healthy files are not touched. Returns whether the file was repaired."""
    data = path.read_bytes()
    lines, damaged = _complete_lines(data)
    if not damaged:
        return False
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    name = "-".join(path.relative_to(path.parents[3]).with_suffix("").parts)  # 2026-10-26-17.jsonl
    (corrupt_dir / f"{name}.gz.{stamp}.orig").write_bytes(data)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wb") as f:
        f.write(b"\n".join(lines) + b"\n" if lines else b"")
    os.replace(tmp, path)
    return True


def repair_recent(root: Path, days: float = REPAIR_DAYS) -> list[Path]:
    """Repair hour files changed in the last `days`. Run when the collector starts, before
    anything appends: a crash is what damages a file, and the collector starts after one."""
    cutoff = time.time() - days * 86400
    repaired = []
    for path in sorted((root / "trades").rglob("*.jsonl.gz")):
        if path.stat().st_mtime >= cutoff and repair_hour_file(path, root / CORRUPT_DIR):
            repaired.append(path)
    return repaired


def load_state(root: Path) -> CollectorState:
    """Where the record is complete to: the state file, else the newest saved trade (a record
    from before the state file), else nothing (a fresh install takes one page and starts there).
    Trades saved at that exact second are remembered so a restart does not save them twice."""
    path = root / STATE_FILE
    if path.exists():
        last: int | None = int(json.loads(path.read_text())["complete_to"])
    else:
        files = sorted((root / "trades").rglob("*.jsonl.gz"))
        last = (
            max((int(t["timestamp"]) for t in _read_lines(files[-1])), default=None)
            if files
            else None
        )
    if last is None:
        return CollectorState()
    hour = _hour_file(root, last)
    rows = _read_lines(hour) if hour.exists() else []
    return CollectorState(last, {trade_key(t): last for t in rows if int(t["timestamp"]) == last})


def _save_state(root: Path, last_ts: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{STATE_FILE}.tmp"
    tmp.write_text(json.dumps({"complete_to": last_ts}) + "\n")
    os.replace(tmp, root / STATE_FILE)


def _fetch(
    client: httpx.Client, cursor: str | None, retry_wait: float
) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, Any] = {"limit": PAGE, "taker_only": "true"}
    if cursor:
        params["cursor"] = cursor
    for attempt in range(RETRIES + 1):
        try:
            resp = client.get(TRADES_PATH, params=params, timeout=30)
        except httpx.TransportError as e:
            err = str(e)
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                err = f"HTTP {resp.status_code}"
            else:
                try:
                    resp.raise_for_status()
                    body = resp.json()
                    more = body["pagination"]["has_more"]
                    return body["data"], body["pagination"]["next_cursor"] if more else None
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                    raise FeedError(str(e)) from e
        if attempt < RETRIES:
            time.sleep(retry_wait * 2**attempt)
    raise FeedError(err)


def _append(root: Path, trades: list[dict[str, Any]]) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    by_file: dict[Path, list[str]] = {}
    for t in sorted(trades, key=lambda t: int(t["timestamp"])):
        line = json.dumps({**t, "collected_at": now}, separators=(",", ":"))
        by_file.setdefault(_hour_file(root, int(t["timestamp"])), []).append(line)
    for path, lines in by_file.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "at") as f:
            f.write("\n".join(lines) + "\n")


def poll_once(
    client: httpx.Client,
    root: Path,
    state: CollectorState,
    max_pages: int = MAX_PAGES,
    retry_wait: float = 2.0,
) -> PollResult:
    """Page back from the newest trade to `state.last_ts`, saving each page as it comes. If a
    request keeps failing, the trades saved so far stay and the mark does not move, so the next
    poll walks the same stretch again (the loader keeps each trade once)."""
    cursor: str | None = None
    newest = oldest = None
    new = pages = 0
    reached = False
    while pages < max_pages:
        try:
            page, cursor = _fetch(client, cursor, retry_wait)
        except FeedError as e:
            return PollResult(new, error=str(e), pages=pages)
        pages += 1
        if not page:
            reached = True
            break
        trades = [{V1_NAMES.get(k, k): v for k, v in t.items()} for t in page]
        stamps = [int(t["timestamp"]) for t in trades]
        newest = max(stamps) if newest is None else newest
        oldest = min(stamps)
        keep_from = newest - KEEP_SEEN_S
        fresh = [
            t
            for t in trades
            if (state.last_ts is None or int(t["timestamp"]) >= state.last_ts)
            and trade_key(t) not in state.seen
        ]
        for t in fresh:
            if int(t["timestamp"]) >= keep_from:
                state.seen[trade_key(t)] = int(t["timestamp"])
        _append(root, fresh)
        new += len(fresh)
        if state.last_ts is None or oldest <= state.last_ts:
            reached = True
            break
        if cursor is None:
            break
    if newest is None:  # an empty feed: nothing to save, nothing missed
        return PollResult(new, pages=pages)
    gap = not reached
    if gap:  # trades between the last saved one and the oldest reached are lost for good
        root.mkdir(parents=True, exist_ok=True)
        with (root / "gaps.jsonl").open("a") as f:
            detected = datetime.now(UTC).isoformat(timespec="seconds")
            f.write(
                json.dumps({"from_ts": state.last_ts, "to_ts": oldest, "detected_at": detected})
                + "\n"
            )
    state.last_ts = max(newest, state.last_ts or newest)
    _save_state(root, state.last_ts)
    cutoff = state.last_ts - KEEP_SEEN_S
    state.seen = {k: ts for k, ts in state.seen.items() if ts >= cutoff}
    return PollResult(new, gap=gap, pages=pages)


@dataclass
class FillResult:
    saved: int
    filled: int
    unfilled: int
    pages: int
    error: str | None = None


def _read_gaps(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _mark_filled(root: Path, gaps: list[dict[str, Any]]) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with (root / FILLED_FILE).open("a") as f:
        for g in gaps:
            f.write(json.dumps({"from_ts": g["from_ts"], "to_ts": g["to_ts"], "filled_at": now}))
            f.write("\n")


def fill_gaps(
    client: httpx.Client,
    root: Path,
    max_pages: int = FILL_MAX_PAGES,
    retry_wait: float = 2.0,
) -> FillResult:
    """Walk the feed back past the oldest recorded gap not yet filled, save the trades inside
    each gap, and append each gap to gaps_filled.jsonl as soon as the walk is past it (pages are
    contiguous, so its trades are all saved by then). gaps.jsonl is left as it is (the collector
    appends to it). Trades at a gap's edges may be saved twice; the loader keeps each trade once."""
    done = {(g["from_ts"], g["to_ts"]) for g in _read_gaps(root / FILLED_FILE)}
    pending = [g for g in _read_gaps(root / "gaps.jsonl") if (g["from_ts"], g["to_ts"]) not in done]
    total = len(pending)
    cursor: str | None = None
    saved = pages = 0
    error = None
    while pending and pages < max_pages:
        try:
            page, cursor = _fetch(client, cursor, retry_wait)
        except FeedError as e:
            error = str(e)
            break
        pages += 1
        if not page:
            break
        trades = [{V1_NAMES.get(k, k): v for k, v in t.items()} for t in page]
        stamps = [int(t["timestamp"]) for t in trades]
        lo, hi = min(stamps), max(stamps)
        spans = [
            (g["from_ts"], g["to_ts"]) for g in pending if g["from_ts"] <= hi and g["to_ts"] >= lo
        ]
        inside = [t for t in trades if any(a <= int(t["timestamp"]) <= b for a, b in spans)]
        _append(root, inside)
        saved += len(inside)
        _mark_filled(root, [g for g in pending if lo <= int(g["from_ts"])])
        pending = [g for g in pending if lo > int(g["from_ts"])]
        if cursor is None:
            break
    return FillResult(saved, total - len(pending), len(pending), pages, error)


def run_collector(root: Path, interval_s: float = COLLECT_EVERY_S) -> None:
    """Poll forever, starting from where the record on disk is complete. Logs every catch-up,
    gap and error, and a line every 3 polls."""
    for path in repair_recent(root):
        log.warning("polymarket: repaired %s after a crash (original in %s/)", path, CORRUPT_DIR)
    state = load_state(root)
    if state.last_ts is not None:
        since = datetime.fromtimestamp(state.last_ts, UTC).isoformat(timespec="seconds")
        log.info("polymarket: record complete to %s; catching up from there", since)
    client = httpx.Client(base_url=BASE_URL, headers={"User-Agent": "gridcase"})
    polls = saved = 0
    while True:
        r = poll_once(client, root, state)
        polls += 1
        saved += r.new
        if r.error:
            log.warning("polymarket poll failed after %d pages: %s", r.pages, r.error)
        if r.gap:
            log.warning("polymarket gap: the feed no longer reached the last saved trade")
        if r.pages > 20:
            log.info("polymarket: caught up %d trades over %d pages", r.new, r.pages)
        if polls % 3 == 0:
            log.info("polymarket: %d trades saved in the last 3 polls", saved)
            saved = 0
        time.sleep(interval_s)
