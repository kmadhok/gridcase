"""Conclusion schema: claims with receipt citations and a tier."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal["governed", "derived", "inferred"]
Status = Literal["open", "supported", "refuted", "confirmed", "superseded"]


class Claim(BaseModel):
    text: str
    receipt_ids: list[str] = Field(default_factory=list)
    tier: Tier = "derived"


class HypothesisVerdict(BaseModel):
    id: str
    status: Status
    confidence: float
    margin: float | None = None
    coverage: float | None = None  # share of the miss this mechanism can account for


class Conclusion(BaseModel):
    case_id: str
    top_hypothesis: str | None
    ranked: list[HypothesisVerdict]
    claims: list[Claim]
    caveats: list[str] = Field(default_factory=list)
