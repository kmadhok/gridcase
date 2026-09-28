"""Tiny arithmetic expression language for derived metrics: identifiers, numbers,
+ - * /, parentheses, and the functions greatest/least/abs. Parsed to an AST; emitted to SQL
by the compiler. Nothing here evaluates Python."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

TOKEN = re.compile(r"\s*(?:(\d+\.\d+|\d+)|([A-Za-z_][A-Za-z0-9_]*)|(\*\*|[-+*/(),]))")
FUNCS = {"greatest", "least", "abs"}


class ExprError(ValueError):
    pass


@dataclass(frozen=True)
class Num:
    value: str


@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class Neg:
    operand: Node


@dataclass(frozen=True)
class Bin:
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Call:
    fn: str
    args: tuple[Node, ...]


Node = Num | Ref | Neg | Bin | Call


def _tokenize(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == "":
                break
            raise ExprError(f"bad token at {pos}: {text[pos : pos + 10]!r}")
        num, ident, op = m.groups()
        if num:
            out.append(("num", num))
        elif ident:
            out.append(("id", ident))
        elif op == "**":
            raise ExprError("exponent is not supported")
        else:
            out.append(("op", op))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.t = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, kind: str, value: str | None = None) -> str:
        tok = self.peek()
        if tok is None or tok[0] != kind or (value is not None and tok[1] != value):
            raise ExprError(f"expected {value or kind} at token {self.i}")
        self.i += 1
        return tok[1]

    def expr(self) -> Node:
        node = self.term()
        while (tok := self.peek()) and tok == ("op", "+") or tok == ("op", "-"):
            self.i += 1
            node = Bin(tok[1], node, self.term())
        return node

    def term(self) -> Node:
        node = self.factor()
        while (tok := self.peek()) and tok == ("op", "*") or tok == ("op", "/"):
            self.i += 1
            node = Bin(tok[1], node, self.factor())
        return node

    def factor(self) -> Node:
        tok = self.peek()
        if tok is None:
            raise ExprError("unexpected end of expression")
        if tok == ("op", "-"):
            self.i += 1
            return Neg(self.factor())
        if tok == ("op", "("):
            self.i += 1
            node = self.expr()
            self.take("op", ")")
            return node
        if tok[0] == "num":
            self.i += 1
            return Num(tok[1])
        if tok[0] == "id":
            self.i += 1
            if self.peek() == ("op", "("):
                if tok[1] not in FUNCS:
                    raise ExprError(f"unknown function {tok[1]}")
                self.i += 1
                args = [self.expr()]
                while self.peek() == ("op", ","):
                    self.i += 1
                    args.append(self.expr())
                self.take("op", ")")
                return Call(tok[1], tuple(args))
            return Ref(tok[1])
        raise ExprError(f"unexpected token {tok[1]!r}")


def parse_expr(text: str) -> Node:
    p = _Parser(_tokenize(text))
    node = p.expr()
    if p.peek() is not None:
        raise ExprError(f"trailing input at token {p.i}")
    return node


def identifiers(node: Node) -> set[str]:
    if isinstance(node, Ref):
        return {node.name}
    if isinstance(node, Neg):
        return identifiers(node.operand)
    if isinstance(node, Bin):
        return identifiers(node.left) | identifiers(node.right)
    if isinstance(node, Call):
        return set().union(*(identifiers(a) for a in node.args))
    return set()


def to_sql(node: Node, ref: Callable[[str], str]) -> str:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Ref):
        return ref(node.name)
    if isinstance(node, Neg):
        return f"(-{to_sql(node.operand, ref)})"
    if isinstance(node, Bin):
        return f"({to_sql(node.left, ref)} {node.op} {to_sql(node.right, ref)})"
    return f"{node.fn}({', '.join(to_sql(a, ref) for a in node.args)})"
