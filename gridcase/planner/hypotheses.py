"""Hypothesis templates and priors, loaded from ontology/hypotheses.yaml and priors.yaml."""

from __future__ import annotations

from datetime import date
from functools import cache
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from gridcase.config import ONTOLOGY_DIR
from gridcase.planner.falsifier import parse
from gridcase.tools.registry import TOOL_NAMES

Season = str  # winter | spring | summer | fall


class Prior(BaseModel):
    a: float
    b: float

    def mean(self) -> float:
        return self.a / (self.a + self.b)


class TestStep(BaseModel):
    tool: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class HypothesisTemplate(BaseModel):
    id: str
    mechanism: str
    preconditions: list[str]
    tests: list[TestStep]
    falsifier: str
    prior: Prior
    mandatory: bool = False

    @model_validator(mode="after")
    def _valid(self) -> HypothesisTemplate:
        parse(self.falsifier)
        for s in self.tests:
            if s.tool not in TOOL_NAMES:
                raise ValueError(f"{self.id}: unknown tool {s.tool}")
        return self


@cache
def load_templates() -> list[HypothesisTemplate]:
    doc = yaml.safe_load((ONTOLOGY_DIR / "hypotheses.yaml").read_text())
    return [HypothesisTemplate(**t) for t in doc["templates"]]


def template_by_id(hid: str) -> HypothesisTemplate:
    for t in load_templates():
        if t.id == hid:
            return t
    raise KeyError(hid)


def season_of(d: date) -> Season:
    return {
        12: "winter",
        1: "winter",
        2: "winter",
        3: "spring",
        4: "spring",
        5: "spring",
        6: "summer",
        7: "summer",
        8: "summer",
        9: "fall",
        10: "fall",
        11: "fall",
    }[d.month]


@cache
def _priors_doc() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((ONTOLOGY_DIR / "priors.yaml").read_text())
    return doc


def prior_for(template_id: str, ba: str, season: Season) -> Prior:
    doc = _priors_doc()
    if template_id not in doc["defaults"]:
        raise KeyError(template_id)
    override = doc.get("overrides", {}).get(template_id, {}).get(ba, {}).get(season)
    return Prior(**(override or doc["defaults"][template_id]))
