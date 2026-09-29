"""identity.balance: residual D - (NG - TI) over the window and its robust z-score against
the preceding 90 days."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np

from gridcase.tools.base import ToolCtx, ToolError, knowable_hours, tool
from gridcase.tools.registry import EntityWindow


@tool("identity.balance")
def identity_balance(ctx: ToolCtx, inputs: EntityWindow) -> tuple[dict[str, Any], float | None]:
    start, _ = inputs.window
    resid = {
        t: float(v)
        for t, v in ctx.metric("balance_residual", inputs.entity, inputs.window)
        if v is not None
    }
    if not resid:
        raise ToolError(f"no D/NG/TI triples for {inputs.entity} in window")
    hist = [
        float(v)
        for _, v in ctx.metric(
            "balance_residual", inputs.entity, (start - timedelta(days=90), start)
        )
        if v is not None
    ]
    demand = [
        float(v) for _, v in ctx.metric("demand", inputs.entity, inputs.window) if v is not None
    ]
    ref = np.array(hist if len(hist) >= 168 else list(resid.values()))
    center = float(np.median(ref))
    scale = max(
        1.4826 * float(np.median(np.abs(ref - center))),
        0.01 * float(np.mean(np.abs(demand))) if demand else 1.0,
    )
    z = {t: (v - center) / scale for t, v in resid.items()}
    worst = max(z, key=lambda t: (abs(z[t]), t))
    return {
        "entity": inputs.entity,
        "n": len(resid),
        "n_missing": max(
            0, knowable_hours(inputs.window, {"eia.D", "eia.NG", "eia.TI"}, ctx.as_of) - len(resid)
        ),
        "mean_residual_mw": sum(resid.values()) / len(resid),
        "max_abs_z": abs(z[worst]),
        "worst_t": worst,
        "history_hours": len(hist),
        "hours": [{"t": t, "residual_mw": resid[t], "z": z[t]} for t in sorted(resid)],
    }, abs(z[worst])
