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
from gridcase.planner.context import PlanContext, default_windows
from gridcase.planner.hypotheses import load_templates
from gridcase.planner.schema import InvestigationPlan
from gridcase.tools.registry import TOOL_INPUTS

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


def default_plan_response(case_id: str, entity: str, as_of: Any) -> InvestigationPlan:
    """A template-default plan built without a store, for tests and validator examples."""
    from gridcase.planner.planner import default_plan
    from gridcase.semantic.ontology import load_ontology

    window, t0, t1 = default_windows(as_of)
    hubs = load_ontology().hubs(entity)
    ctx = PlanContext(
        case_id=case_id,
        entity=entity,
        metric="forecast_error_mw",
        as_of=as_of,
        window=window,
        t0=t0,
        t1=t1,
        event_start=window[0],
        event_end=window[1],
        peers=[r for r in config.REGIONS if r not in (entity, "US48")],
        hub=hubs[0] if hubs else None,
        season="winter",
        anomaly={},
        applicable=[t.id for t in load_templates()],
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


PLAN_SYSTEM = """You plan investigations of plan-vs-actual misses on the US electric grid.
Return an InvestigationPlan. Rules the validator enforces: H0_DATA_ARTIFACT must be first and
tested; test at least two hypotheses; use only the listed tools with exactly their input
schemas; every hypothesis needs a step using a tool its falsifier reads; steps form a DAG;
at most budget_steps steps. Prefer the fewest steps that can falsify each hypothesis. Never
write SQL. Use the provided windows and instants verbatim."""

EXPLAIN_SYSTEM = """You write the conclusion of a grid investigation as a list of claims.
Every claim must cite receipt ids from the ledger given to you, and every number you write
must appear in a cited receipt (rounded to 1%). Uncited claims are allowed only as
tier=inferred and should be rare. Do not invent causes; the ranked hypotheses are given.
If H4_LOAD_SHED is not refuted, add the served-load caveat."""


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
        tools = {name: model.model_json_schema() for name, model in TOOL_INPUTS.items()}
        templates = [t.model_dump() for t in load_templates() if t.id in ctx.applicable]
        user = json.dumps(
            {
                "context": ctx.model_dump(mode="json"),
                "tools": tools,
                "hypothesis_templates": templates,
                "validator_feedback": feedback,
            },
            default=str,
        )
        plan, usage = self._parse(PLAN_SYSTEM, user, InvestigationPlan)
        return (plan if isinstance(plan, InvestigationPlan) else None), usage

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
