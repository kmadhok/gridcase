"""Hypothesis templates: the eight mechanisms, their tests, falsifiers and priors."""

from __future__ import annotations

from datetime import date

import pytest

from gridcase.planner.falsifier import parse
from gridcase.planner.hypotheses import load_templates, prior_for, season_of
from gridcase.tools.registry import TOOL_NAMES

EXPECTED = [
    "H0_DATA_ARTIFACT",
    "H1_WEATHER_DEMAND",
    "H2_SUPPLY_SHORTFALL",
    "H3_IMPORT_CONSTRAINT",
    "H4_LOAD_SHED",
    "H5_SUBREGION_CONCENTRATION",
    "H6_RENEWABLE_FORECAST_MISS",
    "H7_CALENDAR",
]


def test_templates_are_the_eight_in_spec_order() -> None:
    assert [t.id for t in load_templates()] == EXPECTED


def test_every_falsifier_parses_and_every_test_names_a_registry_tool() -> None:
    for t in load_templates():
        parse(t.falsifier)
        assert t.tests and all(s.tool in TOOL_NAMES for s in t.tests)
        assert t.preconditions, t.id


def test_h0_is_mandatory_and_tests_data_quality_first() -> None:
    h0 = load_templates()[0]
    assert h0.mandatory is True
    assert {s.tool for s in h0.tests} >= {"identity.balance", "revision.diff"}
    assert all(t.mandatory is False for t in load_templates()[1:])


def test_seasons() -> None:
    assert season_of(date(2021, 2, 15)) == "winter"
    assert season_of(date(2023, 7, 20)) == "summer"
    assert season_of(date(2023, 5, 1)) == "spring"
    assert season_of(date(2022, 10, 1)) == "fall"


def test_priors_have_ba_season_overrides_and_a_default() -> None:
    p = prior_for("H1_WEATHER_DEMAND", "ERCO", "summer")
    d = prior_for("H1_WEATHER_DEMAND", "US48", "spring")
    assert p.a > 0 and p.b > 0 and d.a > 0
    assert p.mean() > prior_for("H7_CALENDAR", "ERCO", "summer").mean()


def test_unknown_template_prior_raises() -> None:
    with pytest.raises(KeyError):
        prior_for("H9_NOPE", "ERCO", "summer")
