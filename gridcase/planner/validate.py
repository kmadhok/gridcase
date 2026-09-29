"""Plan validator: pure rules, no LLM. One error code per rule; all violations are reported."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gridcase.planner.falsifier import Cmp, Rule, parse
from gridcase.planner.hypotheses import load_templates
from gridcase.planner.schema import InvestigationPlan
from gridcase.semantic.ontology import load_ontology
from gridcase.tools.registry import TOOL_NAMES

H0 = "H0_DATA_ARTIFACT"


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


def _known_entities() -> set[str]:
    onto = load_ontology()
    ents = set(onto.bas)
    for ba in onto.bas:
        ents.update(onto.hubs(ba))
    return ents


def _entity_refs(inputs: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("entity", "hub"):
        if isinstance(inputs.get(key), str):
            refs.append(inputs[key])
    peers = inputs.get("peers")
    if isinstance(peers, list):
        refs.extend(p for p in peers if isinstance(p, str))
    return refs


def validate_plan(plan: InvestigationPlan) -> list[PlanError]:
    errors: list[PlanError] = []
    templates = {t.id: t for t in load_templates()}
    known = _known_entities()

    if H0 not in plan.hypotheses:
        errors.append(PlanError("E_NO_H0", f"{H0} must be tested"))
    elif plan.hypotheses[0] != H0:
        errors.append(PlanError("E_H0_NOT_FIRST", f"{H0} must be first"))
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
        for ref in _entity_refs(s.inputs):
            if ref not in known:
                errors.append(PlanError("E_UNKNOWN_ENTITY", f"unknown entity {ref}", s.step_id))
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
