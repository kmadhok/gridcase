"""Plan step: ask the LLM for an InvestigationPlan, validate, retry with the errors as
feedback up to three times, then fall back to the templates' default test order."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gridcase.planner.context import PlanContext, fill_inputs
from gridcase.planner.hypotheses import load_templates
from gridcase.planner.schema import InvestigationPlan, ToolStep
from gridcase.planner.validate import validate_plan

if TYPE_CHECKING:
    from gridcase.planner.llm import LLM, LLMUsage

MAX_ATTEMPTS = 3


def _key(inputs: dict[str, Any]) -> str:
    return repr(sorted((k, repr(v)) for k, v in inputs.items()))


def default_plan(ctx: PlanContext, budget: int = 12) -> InvestigationPlan:
    """H0 first, then templates greedily by (prior, fewest new steps); identical (tool,
    inputs) steps are shared between hypotheses. A template that does not fit the budget is
    skipped and later, cheaper ones are still tried."""
    templates = {t.id: t for t in load_templates()}
    remaining = [h for h in ctx.applicable if h in templates and h != "H0_DATA_ARTIFACT"]
    steps: list[ToolStep] = []
    index: dict[str, ToolStep] = {}
    hypotheses: list[str] = []

    def filled(h: str) -> list[tuple[str, str, dict[str, Any]]]:
        out: list[tuple[str, str, dict[str, Any]]] = []
        for ts in templates[h].tests:
            inputs = fill_inputs(ts.inputs, ctx)
            if any(v is None for v in inputs.values()):
                continue  # e.g. no hub for this BA
            out.append((ts.tool + "|" + _key(inputs), ts.tool, inputs))
        return out

    def new_steps(h: str) -> int:
        return len({k for k, _, _ in filled(h) if k not in index})

    def accept(h: str) -> bool:
        tests = filled(h)
        if not tests or len(steps) + new_steps(h) > budget:
            return False
        for key, tool, inputs in tests:
            if key in index:
                if h not in index[key].hypothesis_ids:
                    index[key].hypothesis_ids.append(h)
                continue
            step = ToolStep(
                step_id=f"s{len(steps) + 1}", tool=tool, inputs=inputs, hypothesis_ids=[h]
            )
            steps.append(step)
            index[key] = step
        hypotheses.append(h)
        return True

    if "H0_DATA_ARTIFACT" in templates:
        accept("H0_DATA_ARTIFACT")
    while remaining:
        best = min(remaining, key=lambda h: (-ctx.priors.get(h, 0.0), new_steps(h), h))
        remaining.remove(best)
        accept(best)
    return InvestigationPlan(
        case_id=ctx.case_id,
        as_of=ctx.as_of,
        hypotheses=hypotheses,
        steps=steps,
        budget_steps=budget,
        source="template_default",
    )


def make_plan(llm: LLM, ctx: PlanContext) -> tuple[InvestigationPlan, int, LLMUsage]:
    from gridcase.planner.llm import LLMUsage

    usage = LLMUsage()
    feedback: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        plan, u = llm.plan(ctx, feedback)
        usage = usage + u
        if plan is None:
            feedback = ["response did not match the InvestigationPlan schema"]
            continue
        plan = plan.model_copy(update={"case_id": ctx.case_id, "as_of": ctx.as_of, "source": "llm"})
        errors = validate_plan(plan)
        if not errors:
            return plan, attempt, usage
        feedback = [f"{e.code}: {e.message}" for e in errors]
    return default_plan(ctx), MAX_ATTEMPTS, usage
