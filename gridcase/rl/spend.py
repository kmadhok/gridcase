"""The M15 spend ledger: every paid model call reserves, then records, its cost.

Append-only JSONL (one line per recorded call), so the totals survive restarts and a crashed
run never forgets what it spent. `reserve` refuses anything that would pass the phase's
allowance or the global cap; callers reserve an upper-bound estimate before a call and record
the actual cost after it.

A reservation counts against the allowance and the cap until it is settled, by
`record(..., reservation=r)` on success or `release(r)` when the call fails, so concurrent
workers cannot each pass the same remaining amount. Outstanding reservations live in memory
only: the guard is per-process, and separate processes see each other's recorded rows only.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GLOBAL_CAP_USD = 150.0
ALLOWANCES: dict[str, float] = {
    "frontier_eval": 35.0,
    "small_eval": 15.0,
    "train": 85.0,
    "reserve": 15.0,
}


class OverBudget(RuntimeError):
    """A reservation would pass the phase allowance or the global cap."""


@dataclass(frozen=True)
class Reservation:
    """An outstanding amount held against a phase until it is recorded or released."""

    id: int
    phase: str
    usd: float


class Spend:
    def __init__(self, path: Path, cap: float = GLOBAL_CAP_USD) -> None:
        self.path = path
        self.cap = cap
        self._lock = threading.Lock()  # reserve, record and release are atomic per instance
        self._held: dict[int, Reservation] = {}
        self._ids = itertools.count(1)

    def _rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(ln) for ln in self.path.read_text().splitlines() if ln.strip()]

    def spent(self, phase: str | None = None) -> float:
        return sum(float(r["usd"]) for r in self._rows() if phase is None or r["phase"] == phase)

    def remaining(self, phase: str) -> float:
        return min(ALLOWANCES[phase] - self.spent(phase), self.cap - self.spent())

    def outstanding(self, phase: str | None = None) -> float:
        """Reserved but not yet recorded or released, in this process."""
        with self._lock:
            return self._outstanding(phase)

    def _outstanding(self, phase: str | None = None) -> float:
        return sum(r.usd for r in self._held.values() if phase is None or r.phase == phase)

    def reserve(self, phase: str, usd: float) -> Reservation:
        """Hold `usd` unless it, plus what is already outstanding, fits in both the phase
        allowance and the cap; raise OverBudget otherwise. Settle the returned reservation with
        `record(..., reservation=r)` or `release(r)`."""
        if phase not in ALLOWANCES:
            raise KeyError(phase)
        with self._lock:
            held = self._outstanding(phase)
            left = min(
                ALLOWANCES[phase] - self.spent(phase) - held,
                self.cap - self.spent() - self._outstanding(),
            )
            if usd > left + 1e-9:
                raise OverBudget(
                    f"{phase}: ${usd:.2f} requested, ${left:.2f} left "
                    f"(phase ${self.spent(phase):.2f}, total ${self.spent():.2f} of "
                    f"${self.cap:.0f}, ${self._outstanding():.2f} outstanding)"
                )
            r = Reservation(next(self._ids), phase, usd)
            self._held[r.id] = r
            return r

    def release(self, reservation: Reservation) -> None:
        """Drop a reservation without recording (the call failed). Releasing twice is a no-op."""
        with self._lock:
            self._held.pop(reservation.id, None)

    @contextmanager
    def reserved(self, phase: str, usd: float) -> Iterator[Reservation]:
        """Reserve for the duration of a paid call; released on exit however the call ends
        (a no-op if it was already settled by `record`)."""
        r = self.reserve(phase, usd)
        try:
            yield r
        finally:
            self.release(r)

    def record(
        self,
        phase: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: float,
        reservation: Reservation | None = None,
    ) -> None:
        """Append the actual cost; settles `reservation` in the same step, so the amount is
        never counted twice or not at all."""
        if phase not in ALLOWANCES:
            raise KeyError(phase)
        with self._lock:
            self._append(phase, model, input_tokens, output_tokens, usd)
            if reservation is not None:
                self._held.pop(reservation.id, None)

    def _append(
        self, phase: str, model: str, input_tokens: int, output_tokens: int, usd: float
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "phase": phase,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usd": round(usd, 6),
        }
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
