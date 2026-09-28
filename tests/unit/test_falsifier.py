"""Falsifiers are boolean rules over tool outputs in a tiny DSL. No eval(), ever."""

from __future__ import annotations

import pytest

from gridcase.planner.falsifier import FalsifierError, evaluate, parse

OUT = {
    "identity.balance": {"max_abs_z": 1.2, "n_missing": 0},
    "bridge.fuel": {"top_share": 0.62, "top_fuel": "natural_gas"},
}


def test_simple_comparison() -> None:
    assert evaluate(parse("identity.balance.max_abs_z < 3"), OUT) is True
    assert evaluate(parse("identity.balance.max_abs_z >= 3"), OUT) is False


def test_and_or_not_with_parentheses() -> None:
    rule = (
        "not (identity.balance.max_abs_z >= 3 or identity.balance.n_missing > 0) "
        "and bridge.fuel.top_share > 0.5"
    )
    assert evaluate(parse(rule), OUT) is True


def test_missing_output_yields_unknown_not_false() -> None:
    assert evaluate(parse("revision.diff.n_changed == 0"), OUT) is None
    # unknown propagates through 'and' unless the other side decides it
    assert (
        evaluate(parse("revision.diff.n_changed == 0 and identity.balance.max_abs_z >= 3"), OUT)
        is False
    )
    assert (
        evaluate(parse("revision.diff.n_changed == 0 or identity.balance.max_abs_z < 3"), OUT)
        is True
    )


def test_string_equality_is_allowed_for_categorical_outputs() -> None:
    assert evaluate(parse("bridge.fuel.top_fuel == 'natural_gas'"), OUT) is True


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os')",
        "identity.balance.max_abs_z < 3; drop",
        "a.b.c <",
        "1 < 2 < 3",
        "x.y.z ** 2 > 1",
    ],
)
def test_rejects_anything_outside_the_grammar(bad: str) -> None:
    with pytest.raises(FalsifierError):
        parse(bad)
