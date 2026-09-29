"""Plan validator: pure rules, no LLM. One error code per rule; all violations are reported."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from gridcase.planner.falsifier import Cmp, Rule, parse
from gridcase.planner.hypotheses import load_templates, mandatory_ids
from gridcase.planner.schema import InvestigationPlan
from gridcase.semantic.kpis import load_kpis
from gridcase.tools.registry import TOOL_INPUTS, TOOL_NAMES


def _input_errors(tool: str, inputs: dict[str, Any]) -> str | None:
    """Pydantic's complaints about a step's inputs, compact enough to feed back to the planner."""
    try:
        TOOL_INPUTS[tool].model_validate(inputs)
    except ValidationError as e:
        return "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or 'inputs'}: {err['msg']}"
            for err in e.errors()[:4]
        )
    return None


@dataclass(frozen=True)
class PlanError:
    code: str
    message: str
    step_id: str | None = None


class PlanInvalid(ValueError):
    def __init__(self, errors: list[PlanError]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e.code}: {e.message}" for e in errors))


def falsifier_tools(rule: Rule) -> set[str]:
    if isinstance(rule, Cmp):
        return {".".join(rule.path.split(".")[:2])}
    if hasattr(rule, "operand"):
        return falsifier_tools(rule.operand)
    return set().union(*(falsifier_tools(p) for p in rule.parts))


def _segment_errors(inputs: dict[str, Any]) -> list[str]:
    """Unknown KPIs, dimensions, or segment values a step refers to."""
    kpis, dims = load_kpis()
    out: list[str] = []
    kpi = inputs.get("kpi")
    if isinstance(kpi, str) and kpi not in kpis:
        out.append(f"unknown KPI {kpi}")
    filters = inputs.get("filters")
    if isinstance(filters, dict):
        for d, v in filters.items():
            allowed = dims[d].values if d in dims else None
            if d not in dims:
                out.append(f"unknown dimension {d}")
            elif allowed is not None and v not in allowed:
                out.append(f"{d} has no segment {v}")
    return out


def validate_plan(plan: InvestigationPlan) -> list[PlanError]:
    errors: list[PlanError] = []
    templates = {t.id: t for t in load_templates()}

    for m in mandatory_ids():  # the data check: always tested, always first
        if m not in plan.hypotheses:
            errors.append(PlanError("E_NO_DATA_CHECK", f"{m} must be tested"))
        elif plan.hypotheses[0] != m:
            errors.append(PlanError("E_DATA_CHECK_NOT_FIRST", f"{m} must be first"))
    if len(plan.hypotheses) < 2:
        errors.append(PlanError("E_TOO_FEW_HYP", "at least two hypotheses are required"))
    for h in plan.hypotheses:
        if h not in templates:
            errors.append(PlanError("E_UNKNOWN_HYPOTHESIS", f"unknown hypothesis {h}"))
    if len(plan.steps) > plan.budget_steps:
        errors.append(
            PlanError("E_OVER_BUDGET", f"{len(plan.steps)} steps > budget {plan.budget_steps}")
        )

    ids = [s.step_id for s in plan.steps]
    for sid in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(PlanError("E_DUP_STEP", f"duplicate step id {sid}", sid))
    id_set = set(ids)
    for s in plan.steps:
        if s.tool not in TOOL_NAMES:
            errors.append(PlanError("E_UNKNOWN_TOOL", f"unknown tool {s.tool}", s.step_id))
        else:
            bad = _input_errors(s.tool, s.inputs)
            if bad:
                errors.append(PlanError("E_BAD_INPUTS", f"inputs for {s.tool}: {bad}", s.step_id))
        for msg in _segment_errors(s.inputs):
            errors.append(PlanError("E_UNKNOWN_SEGMENT", msg, s.step_id))
        for h in s.hypothesis_ids:
            if h not in plan.hypotheses:
                errors.append(
                    PlanError("E_STEP_HYPOTHESIS", f"step tests {h}, not in plan", s.step_id)
                )
        for d in s.depends_on:
            if d not in id_set:
                errors.append(
                    PlanError("E_BAD_DEPENDENCY", f"depends on unknown step {d}", s.step_id)
                )

    if _has_cycle(plan):
        errors.append(PlanError("E_CYCLE", "step dependencies form a cycle"))

    for h in plan.hypotheses:
        t = templates.get(h)
        if t is None:
            continue
        need = falsifier_tools(parse(t.falsifier))
        have = {s.tool for s in plan.steps if h in s.hypothesis_ids}
        if not need & have:
            errors.append(
                PlanError(
                    "E_UNFALSIFIABLE",
                    f"{h} has no step that can falsify it (needs one of {sorted(need)})",
                )
            )
    return errors


def _has_cycle(plan: InvestigationPlan) -> bool:
    deps = {s.step_id: [d for d in s.depends_on] for s in plan.steps}
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        for d in deps.get(n, []):
            if d in deps and visit(d):
                return True
        state[n] = 2
        return False

    return any(visit(n) for n in deps)


def assert_valid(plan: InvestigationPlan) -> None:
    errors = validate_plan(plan)
    if errors:
        raise PlanInvalid(errors)
