"""Seed the series and entities tables from the registry and ontology/entities.yaml."""

from __future__ import annotations

from typing import Any

import duckdb
import yaml

from gridcase.config import ONTOLOGY_DIR
from gridcase.store.models import Entity
from gridcase.store.series import SERIES


def seed_series(conn: duckdb.DuckDBPyConnection) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO series VALUES (?, ?, ?, ?, ?, ?)",
        [[s.series_id, s.name, s.unit, s.source, s.lag_hours, s.history_start] for s in SERIES],
    )


def load_entities() -> list[Entity]:
    """Expand ontology/entities.yaml into flat Entity rows."""
    doc: dict[str, Any] = yaml.safe_load((ONTOLOGY_DIR / "entities.yaml").read_text())
    out: list[Entity] = []
    for ba in doc["balancing_authorities"]:
        out.append(Entity(entity_id=ba["id"], kind="ba", name=ba["name"], tz=ba["tz"]))
        for sub in ba.get("subregions", []):
            out.append(
                Entity(
                    entity_id=f"{ba['id']}/{sub['id']}",
                    kind="subregion",
                    name=sub["name"],
                    parent_id=ba["id"],
                    tz=ba["tz"],
                )
            )
        for peer in ba.get("interchanges_with", []):
            out.append(
                Entity(
                    entity_id=f"{ba['id']}->{peer}",
                    kind="interchange_path",
                    name=f"{ba['id']} to {peer}",
                    parent_id=ba["id"],
                )
            )
        for code in doc["fuel_codes"]:
            out.append(
                Entity(
                    entity_id=f"{ba['id']}/{code['id']}",
                    kind="fuel",
                    name=f"{ba['name']} {code['name']}",
                    parent_id=ba["id"],
                )
            )
        for hub in ba.get("hubs", []):
            out.append(
                Entity(entity_id=hub["id"], kind="hub", name=hub["name"], parent_id=ba["id"])
            )
    for z in doc["weather_zones"]:
        out.append(
            Entity(
                entity_id=z["id"],
                kind="weather_zone",
                name=z["name"],
                parent_id=z["ba"],
                tz=z.get("tz"),
                lat=z["lat"],
                lon=z["lon"],
            )
        )
    return out


def seed_entities(conn: duckdb.DuckDBPyConnection) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO entities VALUES (?, ?, ?, ?, ?, ?, ?)",
        [[e.entity_id, e.kind, e.name, e.parent_id, e.tz, e.lat, e.lon] for e in load_entities()],
    )
