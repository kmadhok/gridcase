"""peer.compare: correlation of the forecast error (%) with each peer BA over the window."""

from __future__ import annotations

from typing import Any

import numpy as np

from gridcase.tools.base import ToolCtx, ToolError, tool
from gridcase.tools.registry import PeerInputs


@tool("peer.compare")
def peer_compare(ctx: ToolCtx, inputs: PeerInputs) -> tuple[dict[str, Any], float | None]:
    mine = {
        t: float(v)
        for t, v in ctx.metric("forecast_error_pct", inputs.entity, inputs.window)
        if v is not None
    }
    if not mine:
        raise ToolError(f"no forecast error for {inputs.entity}")
    corr: dict[str, float | None] = {}
    for peer in sorted(inputs.peers):
        theirs = {
            t: float(v)
            for t, v in ctx.metric("forecast_error_pct", peer, inputs.window)
            if v is not None
        }
        ts = sorted(set(mine) & set(theirs))
        if len(ts) < 6:
            corr[peer] = None
            continue
        a = np.array([mine[t] for t in ts])
        b = np.array([theirs[t] for t in ts])
        corr[peer] = None if a.std() == 0 or b.std() == 0 else float(np.corrcoef(a, b)[0, 1])
    vals = [c for c in corr.values() if c is not None]
    return {
        "entity": inputs.entity,
        "peers": sorted(inputs.peers),
        "corr": corr,
        "max_corr": max(vals) if vals else None,
        "n": len(mine),
    }, max(vals) if vals else None
