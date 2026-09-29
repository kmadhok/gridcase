"""Verifier: a claim survives only if every receipt it cites exists and every number in its
text appears (±1%) somewhere in those receipts' outputs. Uncited claims are kept as
`inferred`. Dates and clock times are not treated as numbers."""

from __future__ import annotations

import re
from typing import Any

from gridcase.explain.schema import Claim, Conclusion
from gridcase.ledger.schema import Receipt

DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?|\b\d{1,2}:\d{2}\b")
NUMBER = re.compile(
    r"(?P<n>-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)"
    r"(?:\s?(?P<s>k|K|M|B|bn|thousand|million|billion)\b)?"
)
TOLERANCE = 0.01
SCALE = {
    "k": 1e3,
    "K": 1e3,
    "thousand": 1e3,
    "M": 1e6,
    "million": 1e6,
    "B": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}


def number_tokens(text: str) -> list[tuple[float, float]]:
    """Each number in the text with the rounding it was written at: '$2.9M' is 2,900,000 give
    or take 50,000 (half its last digit); plain numbers carry no extra rounding."""
    cleaned = DATE_LIKE.sub(" ", text)
    out: list[tuple[float, float]] = []
    for m in NUMBER.finditer(cleaned):
        raw = m.group("n").replace(",", "")
        scale = SCALE.get(m.group("s") or "", 1.0)
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        slack = 0.5 * 10**-decimals * scale if scale != 1.0 else 0.0
        out.append((float(raw) * scale, slack))
    return out


def numbers_in(text: str) -> list[float]:
    return [v for v, _ in number_tokens(text)]


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


def _labels(obj: Any, out: set[str]) -> None:
    """Strings in the evidence that contain digits (segment and market names like '$100-1k')."""
    if isinstance(obj, str):
        if any(ch.isdigit() for ch in obj) and len(obj) <= 200:
            out.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _labels(v, out)
    elif isinstance(obj, list | tuple):
        for v in obj:
            _labels(v, out)


def _without_labels(text: str, labels: set[str]) -> str:
    """Quoting a label from the evidence is not asserting its numbers."""
    for label in sorted(labels, key=len, reverse=True):
        text = text.replace(label, " ")
    return text


def _matches(n: float, values: list[float], slack: float = 0.0) -> bool:
    """Prose drops signs ("fell 8,236" for -8236), so magnitudes are compared; `slack` is the
    rounding the number was written at ($2.9M)."""
    a = abs(n)
    for v in values:
        b = abs(v)
        if a == b or abs(a - b) <= slack:
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
        labels: set[str] = set()
        for rid in c.receipt_ids:
            _labels(receipts[rid].output, labels)
        text = _without_labels(c.text, labels)
        bad = [n for n, slack in number_tokens(text) if not _matches(n, values, slack)]
        if bad:
            dropped.append(f"{c.text[:60]}: number {_fmt(bad[0])} not in cited receipts")
            continue
        kept.append(c)
    return conclusion.model_copy(update={"claims": kept}), dropped
