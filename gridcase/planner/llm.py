"""The two LLM calls: plan and explain. `AnthropicLLM` uses structured outputs so the model
can only return an InvestigationPlan / Conclusion; `FakeLLM` replays canned plans and uses the
template explainer, for tests and the offline benchmark suite. Tokens and cost are logged
per call.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from pydantic import BaseModel

from gridcase import config
from gridcase.explain.explainer import template_conclusion
from gridcase.explain.schema import Conclusion, HypothesisVerdict
from gridcase.ledger.schema import Receipt
from gridcase.planner.context import PlanContext
from gridcase.planner.hypotheses import load_templates
from gridcase.planner.schema import InvestigationPlan
from gridcase.semantic.kpis import FilterValue

log = logging.getLogger("gridcase")


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
            model=other.model or self.model,
        )


class LLM(Protocol):
    calls: int

    def plan(
        self, ctx: PlanContext, feedback: list[str]
    ) -> tuple[InvestigationPlan | None, LLMUsage]: ...

    def explain(
        self,
        case_id: str,
        ranked: list[HypothesisVerdict],
        receipts: list[Receipt],
        context: dict[str, Any],
    ) -> tuple[Conclusion | None, LLMUsage]: ...


def default_plan_response(
    case_id: str,
    kpi: str,
    as_of: Any,
    filters: dict[str, FilterValue] | None = None,
    record_days: int = 30,
) -> InvestigationPlan:
    """A template-default plan built without a store, for tests and validator examples."""
    from gridcase.planner.context import applies, case_window
    from gridcase.planner.planner import default_plan

    filters = filters or {}
    ctx = PlanContext(
        case_id=case_id,
        kpi=kpi,
        filters=filters,
        as_of=as_of,
        window=case_window(as_of, None),
        record_days=record_days,
        applicable=[t.id for t in load_templates() if applies(t, filters, record_days)],
        priors={t.id: t.prior.mean() for t in load_templates()},
    )
    return default_plan(ctx).model_copy(update={"source": "llm"})


class FakeLLM:
    """Canned plans in order, then the template default; explanations from the template
    explainer."""

    def __init__(self, plans: list[InvestigationPlan] | None = None) -> None:
        self.plans = list(plans or [])
        self.calls = 0

    def plan(
        self, ctx: PlanContext, feedback: list[str]
    ) -> tuple[InvestigationPlan | None, LLMUsage]:
        from gridcase.planner.planner import default_plan

        self.calls += 1
        if self.plans:
            return self.plans.pop(0), LLMUsage()
        return default_plan(ctx).model_copy(update={"source": "llm"}), LLMUsage()

    def explain(
        self,
        case_id: str,
        ranked: list[HypothesisVerdict],
        receipts: list[Receipt],
        context: dict[str, Any],
    ) -> tuple[Conclusion | None, LLMUsage]:
        self.calls += 1
        return template_conclusion(case_id, ranked, receipts, context), LLMUsage()


PLAN_SYSTEM = """You are the analyst on Polymarket's growth team. A KPI the team owns moved
outside its usual range; you plan the investigation that explains why. Your goal: explain the
change in one sentence a product manager can act on, naming the segment and market that moved
and whether the change is concentrated or broad-based.
Return an InvestigationPlan. Rules the validator enforces: the data check (D0_DATA_PROBLEM)
must be first and tested; test at least two drivers; use only the listed tools with exactly
their input schemas; every driver needs a step using a tool its falsifier reads; steps form a
DAG; at most budget_steps steps. Prefer the fewest steps that can falsify each driver; you may
drill down (e.g. by.market within the top category) using `filters`. Never write SQL. Use the
provided KPI, segment filters and window verbatim. Every step's `inputs` must be the complete
input object for its tool (windows are {start, end}; filters are a list of {dimension,
value}). `reference_plan` is the template default: adapt, reorder or drop steps, but keep the
input shapes."""

EXPLAIN_SYSTEM = """You write the conclusion of an investigation into why a Polymarket KPI moved,
for the product team, as a list of claims. The first claim is the answer: what moved, by how
much, and the segment or market that drove it (or that it was broad-based). Then the evidence,
then one claim saying what the team should check next (tier=inferred).
Every factual claim must cite receipt ids from the ledger given to you, and every number you
write must appear in a cited receipt (rounded to 1%). Uncited claims are allowed only as
tier=inferred, and must not state numbers. Do not invent causes or outside facts (no news you
were not given); the ranked drivers are given.
Write for a smart reader who is not an analyst: plain sentences, dollars and traders, percent
changes ("3.1 times its usual level", "72% of the increase"), no statistical jargon and no
internal ids (say "one market", "new traders", not D1_ONE_MARKET)."""


class AnthropicLLM:
    def __init__(self, model: str | None = None) -> None:
        import anthropic

        self.model = model or config.PLANNER_MODEL
        self.client = anthropic.Anthropic()
        self.calls = 0

    def _usage(self, response: Any) -> LLMUsage:
        inp, out = int(response.usage.input_tokens), int(response.usage.output_tokens)
        pi, po = config.MODEL_PRICES.get(self.model, (5.0, 25.0))
        return LLMUsage(
            input_tokens=inp,
            output_tokens=out,
            cost_usd=(inp * pi + out * po) / 1e6,
            model=self.model,
        )

    def _parse(self, system: str, user: str, schema: type[BaseModel]) -> tuple[Any, LLMUsage]:
        import anthropic

        self.calls += 1
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except anthropic.APIError as e:
            log.error("LLM call failed: %s", e)
            return None, LLMUsage(model=self.model)
        usage = self._usage(response)
        log.info(
            "LLM %s: %d in / %d out, $%.4f",
            self.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_usd,
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return None, usage
        return getattr(response, "parsed_output", None), usage

    def plan(
        self, ctx: PlanContext, feedback: list[str]
    ) -> tuple[InvestigationPlan | None, LLMUsage]:
        from gridcase.planner.llm_schema import LLM_INPUTS, LLMPlan, from_plan, to_plan
        from gridcase.planner.planner import default_plan

        tools = {name: model.model_json_schema() for name, model in LLM_INPUTS.items()}
        templates = [t.model_dump() for t in load_templates() if t.id in ctx.applicable]
        user = json.dumps(
            {
                "context": ctx.model_dump(mode="json"),
                "tools": tools,
                "hypothesis_templates": templates,
                "reference_plan": from_plan(default_plan(ctx)).model_dump(mode="json"),
                "validator_feedback": feedback,
            },
            default=str,
        )
        plan, usage = self._parse(PLAN_SYSTEM, user, LLMPlan)
        if not isinstance(plan, LLMPlan):
            return None, usage
        return to_plan(plan, ctx.case_id, ctx.as_of), usage

    def explain(
        self,
        case_id: str,
        ranked: list[HypothesisVerdict],
        receipts: list[Receipt],
        context: dict[str, Any],
    ) -> tuple[Conclusion | None, LLMUsage]:
        user = json.dumps(
            {
                "case_id": case_id,
                "ranked": [v.model_dump() for v in ranked],
                "receipts": [
                    {
                        "receipt_id": r.receipt_id,
                        "tool": r.tool,
                        "inputs": r.inputs,
                        "output": r.output,
                    }
                    for r in receipts
                ],
                "context": context,
            },
            default=str,
        )
        conclusion, usage = self._parse(EXPLAIN_SYSTEM, user, Conclusion)
        return (conclusion if isinstance(conclusion, Conclusion) else None), usage
