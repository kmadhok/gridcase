"""Ontology: typed relations and fuel-category versioning live here, not in queries."""

from __future__ import annotations

from datetime import date

import pytest

from gridcase.semantic.ontology import canonical_fuel, load_ontology


def test_ba_has_subregions_and_weather_zones() -> None:
    onto = load_ontology()
    assert len(onto.subregions("ERCO")) == 8
    assert len(onto.subregions("PJM")) == 20
    assert onto.weather_zones("ERCO") == ["wz/austin", "wz/dallas", "wz/houston", "wz/san_antonio"]
    assert len(onto.weather_zones("US48")) == 14  # all zones feed the national aggregate


def test_ba_interchange_peers_and_hubs() -> None:
    onto = load_ontology()
    assert onto.interchange_peers("ERCO") == ["CEN", "SWPP"]
    assert onto.hubs("ERCO") == ["ERCO/HB_HUBAVG"]
    assert onto.hubs("US48") == []


def test_fuel_codes_map_to_canonical_fuels_with_validity_windows() -> None:
    onto = load_ontology()
    assert canonical_fuel(onto, "NG", date(2019, 1, 1)) == "natural_gas"
    assert canonical_fuel(onto, "BAT", date(2025, 1, 1)) == "storage"
    assert canonical_fuel(onto, "PS", date(2025, 1, 1)) == "storage"
    assert canonical_fuel(onto, "WNB", date(2025, 1, 1)) == "wind"
    with pytest.raises(KeyError):
        canonical_fuel(onto, "XYZ", date(2025, 1, 1))


def test_post_2024_codes_are_not_valid_before_the_schema_change() -> None:
    onto = load_ontology()
    with pytest.raises(ValueError, match="not valid"):
        canonical_fuel(onto, "SNB", date(2023, 1, 1))


def test_fuel_entities_for_a_canonical_fuel_list_every_raw_code() -> None:
    onto = load_ontology()
    assert onto.fuel_entities("ERCO", "solar") == ["ERCO/SNB", "ERCO/SUN"]
    assert onto.fuel_entities("ERCO", "storage") == ["ERCO/BAT", "ERCO/OES", "ERCO/PS", "ERCO/UES"]
    assert onto.canonical_fuels() == [
        "coal",
        "geothermal",
        "hydro",
        "natural_gas",
        "nuclear",
        "other",
        "petroleum",
        "solar",
        "storage",
        "wind",
    ]
