"""The plan schema the model fills must survive strict structured outputs: every tool input
has real properties (an untyped dict becomes `additionalProperties: false`, which the model
can only satisfy with `{}`), and it converts to and from the internal plan losslessly."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from anthropic import transform_schema

from gridcase.planner.llm import default_plan_response
from gridcase.planner.llm_schema import LLMPlan, LLMToolStep, from_plan, to_plan, tool_inputs
from gridcase.tools.registry import TOOL_INPUTS, TOOL_NAMES

AS_OF = datetime(2026, 10, 5, 14)


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    return dict(schema["$defs"][ref.rsplit("/", 1)[1]]) if isinstance(ref, str) else node


def test_strict_schema_gives_every_tool_input_real_properties() -> None:
    strict = transform_schema(LLMPlan.model_json_schema())
    steps = _resolve(strict, _resolve(strict, strict["properties"]["steps"])["items"])
    variants = _resolve(strict, steps["properties"]["inputs"])["anyOf"]
    for v in variants:
        props = _resolve(strict, v)["properties"]
        assert "window" in props
        w = _resolve(strict, props["window"])
        assert w["type"] == "object" and set(w["properties"]) == {"start", "end"}
        if "filters" in props:
            item = _resolve(strict, _resolve(strict, props["filters"])["items"])
            assert set(item["properties"]) == {"dimension", "value"}


def test_step_tool_literal_matches_the_registry() -> None:
    lit = LLMToolStep.model_fields["tool"].annotation
    assert lit is not None and sorted(lit.__args__) == TOOL_NAMES


def test_default_plan_round_trips_through_the_llm_schema() -> None:
    plan = default_plan_response("c", "volume_usd", AS_OF, filters={"category": "Sports"})
    back = to_plan(from_plan(plan), case_id="c", as_of=AS_OF)
    assert [s.tool for s in back.steps] == [s.tool for s in plan.steps]
    for a, b in zip(plan.steps, back.steps, strict=True):
        model = TOOL_INPUTS[a.tool]
        assert model.model_validate(b.inputs) == model.model_validate(a.inputs)
    assert back.steps[1].inputs["filters"] == {"category": "Sports"}


def test_filters_become_a_dict_and_timestamps_naive_utc() -> None:
    raw = {
        "kpi": "volume_usd",
        "filters": [{"dimension": "category", "value": "Crypto"}],
        "window": {"start": "2026-10-05T08:00:00Z", "end": "2026-10-05T09:00:00-05:00"},
    }
    out = tool_inputs("by.market", raw)
    assert out["filters"] == {"category": "Crypto"}
    assert out["window"] == ["2026-10-05T08:00:00", "2026-10-05T14:00:00"]
    assert "top_n" not in tool_inputs("kpi.compare", raw)
