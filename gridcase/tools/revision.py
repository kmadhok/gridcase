"""revision.diff: rows whose value changed between two as_of vintages.

Reads the store directly (audited raw SQL) because the question is about vintages, not
metrics. Reports `history: synthetic_only` when every row's vintage is synthetic, i.e. the
store has no real revision history for this range and "no changes" means "unknowable".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gridcase.tools.base import ToolCtx, ToolError, tool
from gridcase.tools.registry import RevisionInputs

LOOKBACK_DAYS = 7


def _q(series: str, entity: str, t: str, lo: str, hi: str) -> str:
    return (
        f"SELECT valid_time, value, vintage FROM as_of(TIMESTAMP '{t}') "
        f"WHERE series_id = '{series}' AND entity_id = '{entity}' "
        f"AND valid_time >= TIMESTAMP '{lo}' AND valid_time <= TIMESTAMP '{hi}' ORDER BY valid_time"
    )


@tool("revision.diff")
def revision_diff(ctx: ToolCtx, inputs: RevisionInputs) -> tuple[dict[str, Any], float | None]:
    if inputs.t_b > ctx.as_of or inputs.t_a > inputs.t_b:
        raise ToolError("need t_a <= t_b <= as_of")
    lo = (inputs.t_a - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    hi = inputs.t_b.strftime("%Y-%m-%d %H:%M:%S")
    a = {
        t: (float(v), vin)
        for t, v, vin in ctx.raw(
            _q(inputs.series, inputs.entity, inputs.t_a.strftime("%Y-%m-%d %H:%M:%S"), lo, hi)
        )
    }
    b = {t: (float(v), vin) for t, v, vin in ctx.raw(_q(inputs.series, inputs.entity, hi, lo, hi))}
    ctx.series_used.add(inputs.series)
    ctx._extend((inputs.t_a - timedelta(days=LOOKBACK_DAYS), inputs.t_b))
    changed = []
    for t in sorted(set(a) & set(b)):
        old, new = a[t][0], b[t][0]
        if old != new:
            rel = abs(new - old) / abs(old) if old else None
            changed.append({"valid_time": t, "old": old, "new": new, "rel_change": rel})
    rels = [c["rel_change"] for c in changed if c["rel_change"] is not None]
    vintages = {vin for _, vin in b.values()}
    return {
        "series": inputs.series,
        "entity": inputs.entity,
        "n_rows": len(b),
        "n_new_rows": len(set(b) - set(a)),
        "n_changed": len(changed),
        "max_rel_change": max(rels) if rels else 0.0,
        "changed": changed,
        "history": "observed"
        if "observed" in vintages
        else ("synthetic_only" if vintages else "empty"),
    }, max(rels) if rels else 0.0
