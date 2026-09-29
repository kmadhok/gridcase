"""Detector knobs (ontology/detector.yaml): per-BA calibration of the anomaly alarm.

A governed config file like the rest of the ontology: changing it changes what the system
calls an anomaly, so every change gets a DECISIONS line.
"""

from __future__ import annotations

from functools import cache
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from gridcase.config import ONTOLOGY_DIR


class DetectorParams(BaseModel):
    # Quantile of the training |z| that becomes the alarm threshold (in sigmas).
    quantile: float = Field(0.995, gt=0.5, lt=1.0)
    # Consecutive exceeding hours needed for an alarm ...
    min_run: int = Field(3, ge=1)
    # ... unless one hour in the run exceeds the threshold by more than this multiple.
    single_hour_multiple: float = Field(2.0, gt=1.0)
    # The calibrated threshold never drops below this many sigmas.
    min_threshold_z: float = Field(3.0, gt=0.0)


class DetectorConfig(BaseModel):
    defaults: DetectorParams
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def for_entity(self, entity: str) -> DetectorParams:
        over = self.overrides.get(entity)
        if not over:
            return self.defaults
        return DetectorParams.model_validate(self.defaults.model_dump() | over)


def parse_detector_config(doc: dict[str, Any]) -> DetectorConfig:
    try:
        cfg = DetectorConfig.model_validate(doc)
        for entity in cfg.overrides:  # validate every override eagerly
            cfg.for_entity(entity)
    except ValidationError as e:
        raise ValueError(f"invalid detector config: {e}") from e
    return cfg


@cache
def _load() -> DetectorConfig:
    doc: dict[str, Any] = yaml.safe_load((ONTOLOGY_DIR / "detector.yaml").read_text())
    return parse_detector_config(doc)


def detector_params(entity: str) -> DetectorParams:
    return _load().for_entity(entity)
