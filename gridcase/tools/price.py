"""price.co_movement: day-ahead hub LMP against the demand forecast error, and cap hours.

Price coverage is patchy (see DECISIONS); `has_data: false` is a legitimate answer and every
falsifier that uses price fields is written to treat it as unknown.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from gridcase.tools.base import ToolCtx, tool
from gridcase.tools.registry import PriceInputs

# Offer caps (USD/MWh) used to count "at the cap" hours: >= 90% of the cap.
OFFER_CAP = {"ERCO": 5000.0, "CISO": 2000.0, "PJM": 2000.0, "US48": 2000.0}


@tool("price.co_movement")
def price_co_movement(ctx: ToolCtx, inputs: PriceInputs) -> tuple[dict[str, Any], float | None]:
    ba = inputs.hub.split("/")[0]
    price = {t: float(v) for t, v in ctx.metric("lmp_da_hub", ba, inputs.window) if v is not None}
    err = {
        t: float(v) for t, v in ctx.metric("forecast_error_mw", ba, inputs.window) if v is not None
    }
    cap = OFFER_CAP.get(ba, 2000.0)
    cap_hours = sum(1 for p in price.values() if p >= 0.9 * cap)
    ts = sorted(set(price) & set(err))
    corr: float | None = None
    if len(ts) >= 6:
        a = np.array([price[t] for t in ts])
        b = np.array([err[t] for t in ts])
        corr = None if a.std() == 0 or b.std() == 0 else float(np.corrcoef(a, b)[0, 1])
    return {
        "hub": inputs.hub,
        "entity": ba,
        "has_data": bool(price),
        "n_price_hours": len(price),
        "cap_usd_per_mwh": cap,
        "cap_hours": cap_hours,
        "max_price": max(price.values()) if price else None,
        "corr": corr,
    }, corr
