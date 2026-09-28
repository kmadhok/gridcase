"""Ontology object: entities plus typed relations and fuel-category versioning."""

from __future__ import annotations

from datetime import date
from functools import cache
from typing import Any

import yaml
from pydantic import BaseModel

from gridcase.config import ONTOLOGY_DIR


class FuelCode(BaseModel):
    code: str
    canonical: str
    valid_from: date
    valid_to: date | None = None

    def valid_at(self, at: date) -> bool:
        return self.valid_from <= at and (self.valid_to is None or at < self.valid_to)


class BA(BaseModel):
    id: str
    name: str
    tz: str
    subregions: list[str]
    peers: list[str]
    hubs: list[str]


class WeatherZone(BaseModel):
    id: str
    ba: str
    lat: float
    lon: float


class Ontology(BaseModel):
    bas: dict[str, BA]
    zones: dict[str, WeatherZone]
    fuel_codes: dict[str, FuelCode]
    served_by_overrides: dict[str, str]

    def subregions(self, ba: str) -> list[str]:
        return self.bas[ba].subregions

    def interchange_peers(self, ba: str) -> list[str]:
        return self.bas[ba].peers

    def hubs(self, ba: str) -> list[str]:
        return self.bas[ba].hubs

    def weather_zones(self, ba: str) -> list[str]:
        if self.served_by_overrides.get(ba) == "all":
            return sorted(self.zones)
        return sorted(z.id for z in self.zones.values() if z.ba == ba)

    def canonical_fuels(self) -> list[str]:
        return sorted({f.canonical for f in self.fuel_codes.values()})

    def fuel_entities(self, ba: str, canonical: str) -> list[str]:
        return sorted(
            f"{ba}/{f.code}" for f in self.fuel_codes.values() if f.canonical == canonical
        )

    def fuel_entity_map(self, ba: str) -> dict[str, str]:
        """entity_id -> canonical fuel, for every raw code, sorted by entity id."""
        return {
            f"{ba}/{f.code}": f.canonical
            for f in sorted(self.fuel_codes.values(), key=lambda f: f.code)
        }


def canonical_fuel(onto: Ontology, code: str, at: date) -> str:
    if code not in onto.fuel_codes:
        raise KeyError(code)
    fc = onto.fuel_codes[code]
    if not fc.valid_at(at):
        raise ValueError(f"fuel code {code} not valid at {at}")
    return fc.canonical


def build_ontology(
    entities: dict[str, Any], fuels: dict[str, Any], relations: dict[str, Any]
) -> Ontology:
    bas = {
        b["id"]: BA(
            id=b["id"],
            name=b["name"],
            tz=b["tz"],
            subregions=sorted(s["id"] for s in b.get("subregions", [])),
            peers=sorted(b.get("interchanges_with", [])),
            hubs=sorted(h["id"] for h in b.get("hubs", [])),
        )
        for b in entities["balancing_authorities"]
    }
    zones = {
        z["id"]: WeatherZone(id=z["id"], ba=z["ba"], lat=z["lat"], lon=z["lon"])
        for z in entities["weather_zones"]
    }
    codes = {c["code"]: FuelCode(**c) for c in fuels["codes"]}
    return Ontology(
        bas=bas, zones=zones, fuel_codes=codes, served_by_overrides=relations.get("served_by", {})
    )


def _read(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((ONTOLOGY_DIR / name).read_text())
    return doc


@cache
def load_ontology() -> Ontology:
    return build_ontology(
        _read("entities.yaml"), _read("fuel_categories.yaml"), _read("relations.yaml")
    )
