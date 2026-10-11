# ruff: noqa: E501  (SQL and table rows read better unwrapped)
"""A small, polite, cached client for Polymarket's public (keyless) read APIs.

Every GET is cached on disk by URL + params (data/feasibility/cache/, gitignored), so a re-run
replays from the cache and costs no requests. Requests are spaced at least `MIN_INTERVAL_S`
apart, and 429/503 answers are retried after their Retry-After. No authenticated or trading
endpoint is ever called.

Hosts used (verified against docs.polymarket.com on 2026-10-11):
  gamma-api.polymarket.com   /markets (closed, end_date_min/max, volume_num_min, order, ...)
  clob.polymarket.com        /prices-history (market=<token id>, startTs, endTs, fidelity=<minutes>)
  data-api.polymarket.com    /v2/prices-history, /v2/resolutions, /v2/tokens, /v2/trades
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "feasibility" / "cache"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
MIN_INTERVAL_S = 0.25  # at most 4 requests a second, far under any published limit


class Api:
    def __init__(self, cache_dir: Path = CACHE_DIR, offline: bool = False) -> None:
        self.cache_dir = cache_dir
        self.offline = offline
        self.client = httpx.Client(timeout=60, headers={"User-Agent": "gridcase-feasibility"})
        self.requests = 0  # network requests made (cache hits excluded)
        self.cache_hits = 0
        self.seconds = 0.0  # wall time spent on network requests
        self._last = 0.0

    def _path(self, url: str, params: dict[str, Any]) -> Path:
        key = json.dumps([url, sorted((k, str(v)) for k, v in params.items())])
        h = hashlib.sha1(key.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        path = self._path(url, params)
        if path.exists():
            self.cache_hits += 1
            return json.loads(path.read_text())["body"]
        if self.offline:
            raise LookupError(f"not cached (offline): {url} {params}")
        for attempt in range(6):
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            t0 = time.monotonic()
            self._last = t0
            try:
                r = self.client.get(url, params=params)
            except httpx.TransportError:
                time.sleep(2**attempt)
                continue
            self.requests += 1
            self.seconds += time.monotonic() - t0
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(float(r.headers.get("retry-after") or 2**attempt))
                continue
            if r.status_code == 400:  # a documented, deterministic refusal: cache it too
                body: Any = {"_error": r.status_code, "_text": r.text[:500]}
            else:
                r.raise_for_status()
                body = r.json()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"url": url, "params": params, "body": body}))
            return body
        raise RuntimeError(f"gave up after retries: {url} {params}")

    def stats(self) -> dict[str, float]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "network_seconds": round(self.seconds, 1),
        }
