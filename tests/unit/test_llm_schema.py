"""The plan schema the model fills must survive strict structured outputs.

Found on the first real-model run (VPS, 2026-09-29): `ToolStep.inputs: dict[str, Any]` becomes
`{"type": "object", "additionalProperties": false}` under the SDK's strict transform, so the
model could only emit `{}` and every step failed. The model now fills typed per-tool inputs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from anthropic import transform_schema

from gridcase.planner.llm import default_plan_response
from gridcase.planner.llm_schema import LLMPlan, LLMToolStep, from_plan, to_plan
from gridcase.tools.registry import TOOL_INPUTS, TOOL_NAMES

AS_OF = datetime(2021, 2, 16, 12)


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if isinstance(ref, str):
        defs = schema["$defs"]
        assert isinstance(defs, dict)
        return dict(defs[ref.rsplit("/", 1)[1]])
    return node


def test_strict_schema_gives_every_tool_input_real_properties() -> None:
    strict = transform_schema(LLMPlan.model_json_schema())
    steps = _resolve(strict, _resolve(strict, strict["properties"]["steps"])["items"])
    inputs = _resolve(strict, steps["properties"]["inputs"])
    variants = inputs.get("anyOf")
    assert isinstance(variants, list) and len(variants) >= 6
    for v in variants:
        props = _resolve(strict, v).get("properties")
        assert isinstance(props, dict) and len(props) >= 2, v
        # windows are {start, end} objects, never an untyped array
        if "window" in props:
            w = _resolve(strict, props["window"])
            assert w.get("type") == "object" and set(w["properties"]) == {"start", "end"}


def test_step_tool_literal_matches_the_registry() -> None:
    lit = LLMToolStep.model_fields["tool"].annotation
    assert lit is not None and sorted(lit.__args__) == TOOL_NAMES


def test_default_plan_round_trips_through_the_llm_schema() -> None:
    plan = default_plan_response("c", "ERCO", AS_OF)
    back = to_plan(from_plan(plan), case_id="c", as_of=AS_OF)
    assert [s.tool for s in back.steps] == [s.tool for s in plan.steps]
    for a, b in zip(plan.steps, back.steps, strict=True):
        model = TOOL_INPUTS[a.tool]
        assert model.model_validate(b.inputs) == model.model_validate(a.inputs)


def test_entity_window_inputs_do_not_grow_extra_fields() -> None:
    """{entity, window} fits both EntityWindow and PeerInputs; conversion adds nothing."""
    step = LLMToolStep.model_validate(
        {
            "step_id": "s",
            "tool": "identity.balance",
            "inputs": {
                "entity": "ERCO",
                "window": {"start": "2021-02-14T00:00", "end": "2021-02-16T00:00"},
            },
            "hypothesis_ids": ["H0_DATA_ARTIFACT"],
            "depends_on": [],
        }
    )
    out = to_plan(LLMPlan(hypotheses=["H0_DATA_ARTIFACT"], steps=[step]), "c", AS_OF)
    assert set(out.steps[0].inputs) == {"entity", "window"}
    assert out.steps[0].inputs["window"] == ["2021-02-14T00:00:00", "2021-02-16T00:00:00"]


def test_model_timestamps_are_normalised_to_naive_utc() -> None:
    """The model writes '...Z' / '+00:00'; the store and tools are naive UTC throughout."""
    step = LLMToolStep.model_validate(
        {
            "step_id": "s",
            "tool": "revision.diff",
            "inputs": {
                "series": "eia.D",
                "entity": "ERCO",
                "t_a": "2021-02-14T00:00:00Z",
                "t_b": "2021-02-15T19:00:00-05:00",
            },
            "hypothesis_ids": ["H0_DATA_ARTIFACT"],
        }
    )
    out = to_plan(LLMPlan(hypotheses=["H0_DATA_ARTIFACT"], steps=[step]), "c", AS_OF)
    assert out.steps[0].inputs["t_a"] == "2021-02-14T00:00:00"
    assert out.steps[0].inputs["t_b"] == "2021-02-16T00:00:00"
    parsed = TOOL_INPUTS["revision.diff"].model_validate(out.steps[0].inputs)
    assert parsed.t_b.tzinfo is None  # type: ignore[attr-defined]
