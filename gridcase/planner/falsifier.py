"""Falsifier DSL: boolean rules over tool outputs.

Grammar (no eval, no attribute access on Python objects):
    expr   := and_e ('or' and_e)*
    and_e  := not_e ('and' not_e)*
    not_e  := 'not' not_e | atom
    atom   := '(' expr ')' | path op value
    path   := ident '.' ident ('.' ident)+        # tool.name.field[.sub]
    op     := < <= > >= == !=
    value  := number | 'string'
Evaluation is three-valued: True / False / None (unknown, when a referenced tool output is
absent). `and` is False if any side is False; `or` is True if any side is True.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

TOKEN = re.compile(
    r"\s*(?:(?P<num>-?\d+\.\d+|-?\d+)|(?P<str>'[^']*')|(?P<path>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)"
    r"|(?P<kw>and|or|not)\b|(?P<op><=|>=|==|!=|<|>)|(?P<par>[()]))"
)
OPS = {"<", "<=", ">", ">=", "==", "!="}


class FalsifierError(ValueError):
    pass


@dataclass(frozen=True)
class Cmp:
    path: str
    op: str
    value: float | str


@dataclass(frozen=True)
class Not:
    operand: Rule


@dataclass(frozen=True)
class And:
    parts: tuple[Rule, ...]


@dataclass(frozen=True)
class Or:
    parts: tuple[Rule, ...]


Rule = Cmp | Not | And | Or


def _tokenize(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise FalsifierError(f"bad token at {pos}: {text[pos : pos + 12]!r}")
        kind = m.lastgroup or ""
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


class _P:
    def __init__(self, toks: list[tuple[str, str]]) -> None:
        self.t, self.i = toks, 0

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def expr(self) -> Rule:
        parts = [self.and_e()]
        while self.peek() == ("kw", "or"):
            self.i += 1
            parts.append(self.and_e())
        return parts[0] if len(parts) == 1 else Or(tuple(parts))

    def and_e(self) -> Rule:
        parts = [self.not_e()]
        while self.peek() == ("kw", "and"):
            self.i += 1
            parts.append(self.not_e())
        return parts[0] if len(parts) == 1 else And(tuple(parts))

    def not_e(self) -> Rule:
        if self.peek() == ("kw", "not"):
            self.i += 1
            return Not(self.not_e())
        return self.atom()

    def atom(self) -> Rule:
        tok = self.peek()
        if tok is None:
            raise FalsifierError("unexpected end")
        if tok == ("par", "("):
            self.i += 1
            node = self.expr()
            if self.peek() != ("par", ")"):
                raise FalsifierError("missing )")
            self.i += 1
            return node
        if tok[0] != "path":
            raise FalsifierError(f"expected a tool.output path, got {tok[1]!r}")
        self.i += 1
        op = self.peek()
        if op is None or op[0] != "op":
            raise FalsifierError("expected comparison operator")
        self.i += 1
        val = self.peek()
        if val is None or val[0] not in ("num", "str"):
            raise FalsifierError("expected a number or 'string'")
        self.i += 1
        value: float | str = float(val[1]) if val[0] == "num" else val[1][1:-1]
        return Cmp(tok[1], op[1], value)


def parse(text: str) -> Rule:
    p = _P(_tokenize(text))
    rule = p.expr()
    if p.peek() is not None:
        raise FalsifierError(f"trailing input: {p.peek()!r}")
    return rule


def _lookup(outputs: dict[str, Any], path: str) -> Any:
    parts = path.split(".")
    tool = ".".join(parts[:2])
    node: Any = outputs.get(tool)
    for key in parts[2:]:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _cmp(left: Any, op: str, right: float | str) -> bool | None:
    if left is None:
        return None
    if isinstance(right, str) or isinstance(left, str):
        if op not in ("==", "!="):
            return None
        return (str(left) == right) if op == "==" else (str(left) != right)
    try:
        lv = float(left)
    except (TypeError, ValueError):
        return None
    return {
        "<": lv < right,
        "<=": lv <= right,
        ">": lv > right,
        ">=": lv >= right,
        "==": lv == right,
        "!=": lv != right,
    }[op]


MARGIN_CLIP = 2.0


def margin(rule: Rule, outputs: dict[str, Any]) -> float:
    """How strongly the rule holds: positive when true, negative when false, scaled by the
    threshold (fuzzy and = min, or = max, not = negate), clipped to +-MARGIN_CLIP.
    Unknown comparisons contribute 0."""
    if isinstance(rule, Cmp):
        left = _lookup(outputs, rule.path)
        if left is None or isinstance(rule.value, str) or isinstance(left, str | bool):
            truth = _cmp(left, rule.op, rule.value)
            return 0.0 if truth is None else (1.0 if truth else -1.0)
        try:
            lv = float(left)
        except (TypeError, ValueError):
            return 0.0
        # A zero threshold has no natural scale: the comparison then carries its sign only.
        scale = abs(rule.value) if rule.value else max(abs(lv), 1.0)
        if rule.op in ("<", "<="):
            m = (rule.value - lv) / scale
        elif rule.op in (">", ">="):
            m = (lv - rule.value) / scale
        elif rule.op == "==":
            m = 1.0 if lv == rule.value else -abs(lv - rule.value) / scale
        else:
            m = -1.0 if lv == rule.value else abs(lv - rule.value) / scale
        return max(-MARGIN_CLIP, min(MARGIN_CLIP, m))
    if isinstance(rule, Not):
        return -margin(rule.operand, outputs)
    parts = [margin(p, outputs) for p in rule.parts]
    return min(parts) if isinstance(rule, And) else max(parts)


def evaluate(rule: Rule, outputs: dict[str, Any]) -> bool | None:
    if isinstance(rule, Cmp):
        return _cmp(_lookup(outputs, rule.path), rule.op, rule.value)
    if isinstance(rule, Not):
        v = evaluate(rule.operand, outputs)
        return None if v is None else not v
    vals = [evaluate(p, outputs) for p in rule.parts]
    if isinstance(rule, And):
        if any(v is False for v in vals):
            return False
        return None if any(v is None for v in vals) else True
    if any(v is True for v in vals):
        return True
    return None if any(v is None for v in vals) else False
