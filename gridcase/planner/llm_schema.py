"""The plan as the model fills it.

`InvestigationPlan.steps[].inputs` is a free dict internally, but strict structured outputs
turn an untyped object into `additionalProperties: false`, which the model can only satisfy
with `{}`. The model therefore fills `LLMPlan`, whose steps carry typed input shapes: windows
as {start, end} objects and segment filters as a list of {dimension, value}. `to_plan`
converts to the internal plan; the validator then re-checks every step's inputs against its
tool's registry model (E_BAD_INPUTS) and every KPI and segment (E_UNKNOWN_SEGMENT).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from gridcase.planner.schema import InvestigationPlan, ToolStep

ToolName = Literal[
    "by.category",
    "by.market",
    "by.odds",
    "by.size",
    "by.trader",
    "data.coverage",
    "kpi.compare",
]


def _naive_utc(t: datetime) -> datetime:
    """The model writes '...Z' or offsets; the store and every tool are naive UTC."""
    return t.astimezone(UTC).replace(tzinfo=None) if t.tzinfo is not None else t


Dt = Annotated[datetime, AfterValidator(_naive_utc)]


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: Dt
    end: Dt


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: str
    value: str


class KpiIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kpi: str
    filters: list[Filter] = Field(default_factory=list)
    window: Window
    baseline_days: int = 7
    top_n: int = 5  # breakdowns only


class CoverageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: Window


StepInputs = KpiIn | CoverageIn

LLM_INPUTS: dict[str, type[BaseModel]] = {
    "kpi.compare": KpiIn,
    "data.coverage": CoverageIn,
    "by.market": KpiIn,
    "by.category": KpiIn,
    "by.size": KpiIn,
    "by.trader": KpiIn,
    "by.odds": KpiIn,
}


class LLMToolStep(BaseModel):
    step_id: str
    tool: ToolName
    inputs: StepInputs
    hypothesis_ids: list[str]
    depends_on: list[str] = Field(default_factory=list)


class LLMPlan(BaseModel):
    hypotheses: list[str]
    steps: list[LLMToolStep]
    budget_steps: int = 12


def _registry_shape(tool: str, raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k == "window" and isinstance(v, dict):
            out[k] = [v["start"], v["end"]]
        elif k == "filters" and isinstance(v, list):
            out[k] = {f["dimension"]: f["value"] for f in v}
        elif k == "top_n" and not tool.startswith("by."):
            continue
        else:
            out[k] = v
    return out


def tool_inputs(tool: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Model-shaped inputs for one tool (as a tool call's JSON) -> registry-shaped inputs."""
    parsed = LLM_INPUTS[tool].model_validate(raw)
    return _registry_shape(tool, parsed.model_dump(mode="json", exclude_unset=True))


def to_plan(plan: LLMPlan, case_id: str, as_of: datetime) -> InvestigationPlan:
    steps = [
        ToolStep(
            step_id=s.step_id,
            tool=s.tool,
            inputs=_registry_shape(s.tool, s.inputs.model_dump(mode="json", exclude_unset=True)),
            hypothesis_ids=s.hypothesis_ids,
            depends_on=s.depends_on,
        )
        for s in plan.steps
    ]
    return InvestigationPlan(
        case_id=case_id,
        as_of=as_of,
        hypotheses=plan.hypotheses,
        steps=steps,
        budget_steps=plan.budget_steps,
        source="llm",
    )


def _model_shape(k: str, v: Any) -> Any:
    if k == "window":
        return {"start": v[0], "end": v[1]}
    if k == "filters" and isinstance(v, dict):
        return [{"dimension": d, "value": x} for d, x in sorted(v.items())]
    return v


def from_plan(plan: InvestigationPlan) -> LLMPlan:
    """The internal plan in the model's shape (the template default shown as a reference)."""
    steps = [
        LLMToolStep(
            step_id=s.step_id,
            tool=s.tool,  # type: ignore[arg-type]
            inputs=LLM_INPUTS[s.tool].model_validate(  # type: ignore[arg-type]
                {k: _model_shape(k, v) for k, v in s.inputs.items()}
            ),
            hypothesis_ids=s.hypothesis_ids,
            depends_on=s.depends_on,
        )
        for s in plan.steps
    ]
    return LLMPlan(hypotheses=plan.hypotheses, steps=steps, budget_steps=plan.budget_steps)
