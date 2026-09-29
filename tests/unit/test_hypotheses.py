"""Drivers (ontology/hypotheses.yaml) and priors (ontology/priors.yaml)."""

from __future__ import annotations

from datetime import date

import pytest

from gridcase.planner.falsifier import parse
from gridcase.planner.hypotheses import load_templates, mandatory_ids, prior_for, season_of
from gridcase.planner.validate import falsifier_tools
from gridcase.tools.registry import TOOL_NAMES


def test_the_seven_product_drivers_in_order() -> None:
    assert [t.id for t in load_templates()] == [
        "D0_DATA_PROBLEM",
        "D1_ONE_MARKET",
        "D2_ONE_CATEGORY",
        "D3_NEW_TRADERS",
        "D4_WHALES",
        "D5_LONGSHOTS",
        "D6_BROAD_BASED",
    ]


def test_every_falsifier_parses_and_reads_a_tool_its_driver_runs() -> None:
    for t in load_templates():
        rule = parse(t.falsifier)
        tested = {s.tool for s in t.tests}
        assert tested <= set(TOOL_NAMES)
        assert falsifier_tools(rule) & tested, t.id


def test_the_data_check_is_the_only_mandatory_driver() -> None:
    assert mandatory_ids() == ["D0_DATA_PROBLEM"]
    d0 = load_templates()[0]
    assert [s.tool for s in d0.tests] == ["data.coverage"]


def test_consequences_and_explains_point_at_real_drivers_and_fields() -> None:
    ids = {t.id for t in load_templates()}
    for t in load_templates():
        assert set(t.consequence_of) <= ids - {t.id}
        if t.explains:
            tool = ".".join(t.explains.split(".")[:2])
            assert tool in {s.tool for s in t.tests}, t.id
    by_id = {t.id: t for t in load_templates()}
    assert "D1_ONE_MARKET" in by_id["D2_ONE_CATEGORY"].consequence_of
    assert "D3_NEW_TRADERS" in by_id["D6_BROAD_BASED"].consequence_of


def test_seasons() -> None:
    assert season_of(date(2026, 1, 5)) == "winter"
    assert season_of(date(2026, 7, 5)) == "summer"


def test_priors_exist_for_every_driver_and_unknown_ones_raise() -> None:
    for t in load_templates():
        assert 0 < prior_for(t.id, "volume_usd", "all").mean() < 1
    with pytest.raises(KeyError):
        prior_for("NOPE", "volume_usd", "all")
