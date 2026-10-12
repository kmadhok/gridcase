"""The M15 spend ledger: every paid model call reserves, then records, its cost.

Append-only JSONL (one line per recorded call), so the totals survive restarts and a crashed
run never forgets what it spent. `reserve` refuses anything that would pass the phase's
allowance or the global cap; callers reserve an upper-bound estimate before a call and record
the actual cost after it.
"""

from __future__ import annotations

import json
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


class Spend:
    def __init__(self, path: Path, cap: float = GLOBAL_CAP_USD) -> None:
        self.path = path
        self.cap = cap

    def _rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(ln) for ln in self.path.read_text().splitlines() if ln.strip()]

    def spent(self, phase: str | None = None) -> float:
        return sum(float(r["usd"]) for r in self._rows() if phase is None or r["phase"] == phase)

    def remaining(self, phase: str) -> float:
        return min(ALLOWANCES[phase] - self.spent(phase), self.cap - self.spent())

    def reserve(self, phase: str, usd: float) -> None:
        """Raise OverBudget unless `usd` more fits in both the phase allowance and the cap."""
        if phase not in ALLOWANCES:
            raise KeyError(phase)
        if usd > self.remaining(phase) + 1e-9:
            raise OverBudget(
                f"{phase}: ${usd:.2f} requested, ${self.remaining(phase):.2f} left "
                f"(phase ${self.spent(phase):.2f}, total ${self.spent():.2f} of ${self.cap:.0f})"
            )

    def record(
        self, phase: str, model: str, input_tokens: int, output_tokens: int, usd: float
    ) -> None:
        if phase not in ALLOWANCES:
            raise KeyError(phase)
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
