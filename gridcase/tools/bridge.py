"""bridge.fuel: additive bridge of net-generation change by canonical fuel between two hours."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gridcase.tools.base import ToolCtx, ToolError, tool
from gridcase.tools.registry import BridgeInputs

RENEWABLE = {"wind", "solar"}


@tool("bridge.fuel")
def bridge_fuel(ctx: ToolCtx, inputs: BridgeInputs) -> tuple[dict[str, Any], float | None]:
    at0 = {
        f: float(v)
        for _, f, v in ctx.metric(
            "generation_by_fuel", inputs.entity, (inputs.t0, inputs.t0 + timedelta(hours=1))
        )
    }
    at1 = {
        f: float(v)
        for _, f, v in ctx.metric(
            "generation_by_fuel", inputs.entity, (inputs.t1, inputs.t1 + timedelta(hours=1))
        )
    }
    if not at0 or not at1:
        raise ToolError(f"no fuel data for {inputs.entity} at t0/t1")
    fuels = sorted(set(at0) | set(at1))
    deltas = {f: at1.get(f, 0.0) - at0.get(f, 0.0) for f in fuels}
    total = sum(deltas.values())
    ranked = sorted(fuels, key=lambda f: (-abs(deltas[f]), f))
    top = ranked[0]
    denom = abs(total)
    renewable = sum(deltas[f] for f in fuels if f in RENEWABLE)
    return {
        "entity": inputs.entity,
        "t0": inputs.t0,
        "t1": inputs.t1,
        "total_change_mw": total,
        "fuels": [
            {
                "fuel": f,
                "mw_t0": at0.get(f, 0.0),
                "mw_t1": at1.get(f, 0.0),
                "delta_mw": deltas[f],
                "share": abs(deltas[f]) / denom if denom else None,
            }
            for f in ranked
        ],
        "top_fuel": top,
        "top_share": abs(deltas[top]) / denom if denom else None,
        "renewable_share": abs(renewable) / denom if denom else None,
    }, total
