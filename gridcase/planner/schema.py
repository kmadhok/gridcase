"""InvestigationPlan: the only thing the planner LLM is allowed to produce."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolStep(BaseModel):
    step_id: str
    tool: str  # must exist in the registry
    inputs: dict[str, Any]
    hypothesis_ids: list[str]  # which hypotheses this step tests
    depends_on: list[str] = Field(default_factory=list)


class InvestigationPlan(BaseModel):
    case_id: str
    as_of: datetime
    hypotheses: list[str]  # template ids, ordered by prior
    steps: list[ToolStep]  # a DAG
    budget_steps: int = 12
    source: Literal["llm", "template_default"] = "llm"
