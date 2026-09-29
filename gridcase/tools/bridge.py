"""bridge.fuel: additive bridge of net-generation change by canonical fuel.

mode=level: fuel levels at t1 minus levels at t0.
mode=expected: fuel levels at t1 minus each fuel's own baseline expectation at t1 (hour-of-
week + temperature model fit before the refit date). This is the shortfall view: during
load shed, generation tracks served load, so level-to-level bridges hide the missing
capacity; expectation-based bridges show which fleet failed to deliver.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from gridcase.tools.base import ToolCtx, ToolError, tool
from gridcase.tools.baseline import fit_baseline, predict
from gridcase.tools.registry import BridgeInputs

RENEWABLE = {"wind", "solar"}


def _levels(ctx: ToolCtx, entity: str, t: datetime) -> dict[str, float]:
    return {
        str(f): float(v)
        for _, f, v in ctx.metric("generation_by_fuel", entity, (t, t + timedelta(hours=1)))
    }


def _expected(
    ctx: ToolCtx, entity: str, t: datetime, fuels: list[str]
) -> tuple[dict[str, float], list[str]]:
    out: dict[str, float] = {}
    unmodelled: list[str] = []
    for f in fuels:
        try:
            fit, _ = fit_baseline(ctx, "generation_by_fuel", entity, dims={"fuel": f})
        except ToolError:
            unmodelled.append(f)
            continue
        out[f] = predict(ctx, fit, entity, (t, t + timedelta(hours=1)))[0]["expected"]
    return out, unmodelled


@tool("bridge.fuel")
def bridge_fuel(ctx: ToolCtx, inputs: BridgeInputs) -> tuple[dict[str, Any], float | None]:
    at1 = _levels(ctx, inputs.entity, inputs.t1)
    if not at1:
        raise ToolError(f"no fuel data for {inputs.entity} at t1")
    unmodelled: list[str] = []
    if inputs.mode == "expected":
        at0, unmodelled = _expected(ctx, inputs.entity, inputs.t1, sorted(at1))
        if not at0:
            raise ToolError(f"no fuel history to model expected output for {inputs.entity}")
        for f in unmodelled:
            at0[f] = at1[f]  # no history: contributes nothing to the bridge
    else:
        at0 = _levels(ctx, inputs.entity, inputs.t0)
        if not at0:
            raise ToolError(f"no fuel data for {inputs.entity} at t0")
    fuels = sorted(set(at0) | set(at1))
    deltas = {f: at1.get(f, 0.0) - at0.get(f, 0.0) for f in fuels}
    total = sum(deltas.values())
    level0 = sum(at0.values())
    ranked = sorted(fuels, key=lambda f: (-abs(deltas[f]), f))
    top = ranked[0]
    # Shares are of gross movement (sum of |delta|): bounded in [0, 1] even when fuels offset
    # each other and the net change is near zero. delta_mw still sums exactly to the total.
    gross = sum(abs(d) for d in deltas.values())
    renewable = sum(deltas[f] for f in fuels if f in RENEWABLE)
    return {
        "entity": inputs.entity,
        "mode": inputs.mode,
        "insufficient": inputs.event_start is not None and inputs.t1 < inputs.event_start,
        "basis": "expected level at t1" if inputs.mode == "expected" else "level at t0",
        "t0": inputs.t0,
        "t1": inputs.t1,
        "total_change_mw": total,
        "total_change_pct": total / level0 if level0 else None,
        "gross_change_mw": gross,
        "fuels": [
            {
                "fuel": f,
                "mw_t0": at0.get(f, 0.0),
                "mw_t1": at1.get(f, 0.0),
                "delta_mw": deltas[f],
                "share": abs(deltas[f]) / gross if gross else None,
            }
            for f in ranked
        ],
        "top_fuel": top,
        "top_delta_mw": deltas[top],
        "top_share": abs(deltas[top]) / gross if gross else None,
        "renewable_delta_mw": renewable,
        "renewable_share": abs(renewable) / gross if gross else None,
        "unmodelled_fuels": unmodelled,
    }, total
