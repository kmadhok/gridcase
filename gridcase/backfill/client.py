"""A polite, cached, thread-safe client for Polymarket's public (keyless) read APIs.

Every GET is cached on disk by URL + params, so a rerun replays from the cache and a long
backfill resumes where it stopped. A shared limiter spaces requests across threads (default
8 a second, under 10% of every published limit: CLOB /prices-history 1,000 per 10 s, data-api
/v2/trades 300 per 10 s, Gamma listings 900 per 10 s; docs.polymarket.com rate limits, read
2026-10-12). 429 and 5xx answers are retried after Retry-After; a 400 is a deterministic
refusal and is cached like an answer. No authenticated or trading endpoint is ever called.

The cache key and layout match experiments/feasibility/api.py, so that cache is reusable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("gridcase")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"


class Client:
    def __init__(
        self,
        cache_dir: Path,
        rate_per_s: float = 8.0,
        offline: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.offline = offline
        self.interval = 1.0 / rate_per_s
        self._http = httpx.Client(
            timeout=60, headers={"User-Agent": "gridcase-backfill"}, transport=transport
        )
        self._lock = threading.Lock()
        self._next = 0.0
        self.requests = 0
        self.cache_hits = 0
        self.retries = 0

    def _path(self, url: str, params: dict[str, Any]) -> Path:
        key = json.dumps([url, sorted((k, str(v)) for k, v in params.items())])
        h = hashlib.sha1(key.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _wait_turn(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        path = self._path(url, params)
        if path.exists():
            with self._lock:
                self.cache_hits += 1
            return json.loads(path.read_text())["body"]
        if self.offline:
            raise LookupError(f"not cached (offline): {url} {params}")
        for attempt in range(7):
            self._wait_turn()
            try:
                r = self._http.get(url, params=params)
            except httpx.TransportError as e:
                log.warning("backfill: transport error %s (attempt %d)", e, attempt + 1)
                with self._lock:
                    self.retries += 1
                time.sleep(min(2**attempt, 60))
                continue
            with self._lock:
                self.requests += 1
            if r.status_code in (429, 500, 502, 503, 504):
                wait = float(r.headers.get("retry-after") or min(2**attempt, 60))
                log.warning("backfill: HTTP %d, retrying in %.0fs", r.status_code, wait)
                with self._lock:
                    self.retries += 1
                time.sleep(wait)
                continue
            if r.status_code == 400:
                body: Any = {"_error": 400, "_text": r.text[:500]}
            else:
                r.raise_for_status()
                body = r.json()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"url": url, "params": params, "body": body}))
            tmp.replace(path)
            return body
        raise RuntimeError(f"gave up after retries: {url} {params}")

    def stats(self) -> dict[str, int]:
        return {"requests": self.requests, "cache_hits": self.cache_hits, "retries": self.retries}
