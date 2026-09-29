"""The plan as the model fills it.

`InvestigationPlan.steps[].inputs` is a free dict internally, but strict structured outputs
turn an untyped object into `additionalProperties: false`, which the model can only satisfy
with `{}`. The model therefore fills `LLMPlan`, whose steps carry one of the typed per-tool
input shapes below (windows as {start, end} objects: an untyped array survives the transform
no better). `to_plan` converts to the internal plan; the validator then re-checks every
step's inputs against its tool's registry model (E_BAD_INPUTS).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from gridcase.planner.schema import InvestigationPlan, ToolStep

ToolName = Literal[
    "anomaly.detect",
    "baseline.expected",
    "bridge.fuel",
    "contribution.subregion",
    "identity.balance",
    "peer.compare",
    "price.co_movement",
    "regress.temperature",
    "revision.diff",
    "variance.plan_vs_actual",
]


def _naive_utc(t: datetime) -> datetime:
    """The model writes '...Z' or offsets; the store and every tool are naive UTC."""
    return t.astimezone(UTC).replace(tzinfo=None) if t.tzinfo is not None else t


Dt = Annotated[datetime, AfterValidator(_naive_utc)]


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: Dt
    end: Dt


# extra="forbid" on every shape: the union then resolves by the fields present instead of
# silently dropping one (an EntityWindow would otherwise swallow a MetricEntityWindow's metric).
class MetricEntityWindowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    entity: str
    window: Window
    event_start: Dt | None = None


class EntityWindowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str
    window: Window
    event_start: Dt | None = None
    event_end: Dt | None = None


class BridgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str
    t0: Dt
    t1: Dt
    mode: Literal["level", "expected"] = "level"
    event_start: Dt | None = None


class RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    series: str
    entity: str
    t_a: Dt
    t_b: Dt


class PeerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str
    peers: list[str]
    window: Window


class PriceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hub: str
    window: Window


StepInputs = MetricEntityWindowIn | EntityWindowIn | BridgeIn | RevisionIn | PeerIn | PriceIn

LLM_INPUTS: dict[str, type[BaseModel]] = {
    "anomaly.detect": MetricEntityWindowIn,
    "baseline.expected": MetricEntityWindowIn,
    "bridge.fuel": BridgeIn,
    "contribution.subregion": EntityWindowIn,
    "identity.balance": EntityWindowIn,
    "peer.compare": PeerIn,
    "price.co_movement": PriceIn,
    "regress.temperature": EntityWindowIn,
    "revision.diff": RevisionIn,
    "variance.plan_vs_actual": EntityWindowIn,
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


def _window_out(v: Any) -> Any:
    return [v["start"], v["end"]] if isinstance(v, dict) and set(v) == {"start", "end"} else v


def to_plan(plan: LLMPlan, case_id: str, as_of: datetime) -> InvestigationPlan:
    steps = []
    for s in plan.steps:
        raw = s.inputs.model_dump(mode="json", exclude_unset=True)
        inputs = {k: _window_out(v) for k, v in raw.items()}
        steps.append(
            ToolStep(
                step_id=s.step_id,
                tool=s.tool,
                inputs=inputs,
                hypothesis_ids=s.hypothesis_ids,
                depends_on=s.depends_on,
            )
        )
    return InvestigationPlan(
        case_id=case_id,
        as_of=as_of,
        hypotheses=plan.hypotheses,
        steps=steps,
        budget_steps=plan.budget_steps,
        source="llm",
    )


def _window_in(k: str, v: Any) -> Any:
    return {"start": v[0], "end": v[1]} if k == "window" else v


def from_plan(plan: InvestigationPlan) -> LLMPlan:
    """The internal plan in the model's shape (used to show the template default as a
    reference in the prompt)."""
    steps = []
    for s in plan.steps:
        model = LLM_INPUTS[s.tool]
        inputs = model.model_validate({k: _window_in(k, v) for k, v in s.inputs.items()})
        steps.append(
            LLMToolStep(
                step_id=s.step_id,
                tool=s.tool,  # type: ignore[arg-type]
                inputs=inputs,  # type: ignore[arg-type]
                hypothesis_ids=s.hypothesis_ids,
                depends_on=s.depends_on,
            )
        )
    return LLMPlan(hypotheses=plan.hypotheses, steps=steps, budget_steps=plan.budget_steps)
