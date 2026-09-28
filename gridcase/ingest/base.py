"""Shared pieces: the Source protocol, a retrying JSON client, and the raw-response cache."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from gridcase.store.models import RawObs

log = logging.getLogger("gridcase")

RETRY_STATUSES = {429, 500, 502, 503, 504}


class IngestError(RuntimeError):
    """Raised by a source after retries are exhausted. The runner records it and moves on."""


class Source(Protocol):
    name: str

    def chunks(self, start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
        """Split [start, end) into request-sized windows."""
        ...

    def fetch(self, start: datetime, end: datetime) -> list[RawObs]:
        """Rows for [start, end). Must raise IngestError on unrecoverable failure."""
        ...


def day_chunks(start: datetime, end: datetime, days: int) -> Iterator[tuple[datetime, datetime]]:
    t = start
    while t < end:
        nxt = min(t + timedelta(days=days), end)
        yield t, nxt
        t = nxt


def row_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]


class RetryingClient:
    """GET JSON with spacing, backoff on 429/5xx and transport errors. Never logs secrets."""

    def __init__(
        self,
        client: httpx.Client,
        min_interval_s: float = 0.25,
        max_attempts: int = 5,
        retry_sleep: float = 1.0,
        secret: str | None = None,
    ) -> None:
        self.client = client
        self.min_interval_s = min_interval_s
        self.max_attempts = max_attempts
        self.retry_sleep = retry_sleep
        self.secret = secret
        self._last = 0.0

    def _redact(self, text: str) -> str:
        return text.replace(self.secret, "<redacted>") if self.secret else text

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        for attempt in range(1, self.max_attempts + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.client.get(url, params=params, timeout=90)
            except httpx.TransportError as e:
                log.warning(
                    "transport error (%d/%d) on %s: %s",
                    attempt,
                    self.max_attempts,
                    url,
                    self._redact(str(e)),
                )
                time.sleep(self.retry_sleep * 2**attempt)
                continue
            if r.status_code in RETRY_STATUSES:
                log.warning(
                    "HTTP %d (%d/%d) on %s; backing off",
                    r.status_code,
                    attempt,
                    self.max_attempts,
                    url,
                )
                time.sleep(self.retry_sleep * 2**attempt)
                continue
            if r.status_code != 200:
                raise IngestError(f"HTTP {r.status_code} on {url}: {self._redact(r.text[:200])}")
            return r.json()
        raise IngestError(
            f"gave up after {self.max_attempts} attempts on {url} (last status was 429/5xx)"
        )


class RawCache:
    """Cache raw API bodies under data/raw/{source}/{route}/{key}_{start}_{end}.json."""

    def __init__(self, raw_dir: Path | None, source: str) -> None:
        self.dir = raw_dir / source if raw_dir else None

    def path(self, route: str, key: str, start: datetime, end: datetime) -> Path | None:
        if self.dir is None:
            return None
        safe_key = key.replace("/", "_")
        return self.dir / route / f"{safe_key}_{start:%Y%m%dT%H}_{end:%Y%m%dT%H}.json"

    def get(self, p: Path | None) -> Any | None:
        if p is None or not p.exists():
            return None
        return json.loads(p.read_text())

    def put(self, p: Path | None, body: Any) -> None:
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body))
