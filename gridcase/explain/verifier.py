"""Verifier: a claim survives only if every receipt it cites exists and every number in its
text appears (±1%) somewhere in those receipts' outputs. Uncited claims are kept as
`inferred`. Dates and clock times are not treated as numbers."""

from __future__ import annotations

import re
from typing import Any

from gridcase.explain.schema import Claim, Conclusion
from gridcase.ledger.schema import Receipt

DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?|\b\d{1,2}:\d{2}\b")
NUMBER = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")
TOLERANCE = 0.01


def numbers_in(text: str) -> list[float]:
    cleaned = DATE_LIKE.sub(" ", text)
    return [float(m.group().replace(",", "")) for m in NUMBER.finditer(cleaned)]


def _flatten(obj: Any, out: list[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int | float):
        out.append(float(obj))
        if 0 <= obj <= 1:
            out.append(float(obj) * 100)  # shares may be written as percentages
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten(v, out)
    elif isinstance(obj, list | tuple):
        for v in obj:
            _flatten(v, out)


def _matches(n: float, values: list[float]) -> bool:
    """Prose drops signs ("fell 8,236 MW" for -8236), so magnitudes are compared."""
    a = abs(n)
    for v in values:
        b = abs(v)
        if a == b:
            return True
        if abs(a - b) <= TOLERANCE * max(a, b, 1e-9):
            return True
        if abs(a - b) < 0.5 and b < 100:  # rounding to whole units on small numbers
            return True
    return False


def _fmt(n: float) -> str:
    return str(int(n)) if n.is_integer() else str(n)


def verify(
    conclusion: Conclusion,
    receipts: dict[str, Receipt],
    drop_uncited_numbers: bool = False,
) -> tuple[Conclusion, list[str]]:
    """drop_uncited_numbers: an uncited sentence that states a figure is dropped instead of
    kept as inferred (the question box: interpretation may explain, not assert numbers)."""
    kept: list[Claim] = []
    dropped: list[str] = []
    for c in conclusion.claims:
        if not c.receipt_ids:
            if drop_uncited_numbers and numbers_in(c.text):
                dropped.append(f"{c.text[:60]}: states a number without evidence")
                continue
            kept.append(c.model_copy(update={"tier": "inferred"}))
            continue
        missing = [rid for rid in c.receipt_ids if rid not in receipts]
        if missing:
            dropped.append(f"{c.text[:60]}: unknown receipt {missing[0]}")
            continue
        values: list[float] = []
        for rid in c.receipt_ids:
            _flatten(receipts[rid].output, values)
            effect = receipts[rid].effect_size
            if effect is not None:
                values.append(float(effect))
        bad = [n for n in numbers_in(c.text) if not _matches(n, values)]
        if bad:
            dropped.append(f"{c.text[:60]}: number {_fmt(bad[0])} not in cited receipts")
            continue
        kept.append(c)
    return conclusion.model_copy(update={"claims": kept}), dropped
