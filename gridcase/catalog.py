"""The KPI catalog: every KPI a reader may meet, in plain words, with its governed definition,
whether it is watched, and its value over the last complete hour."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import duckdb

from gridcase.market_alarm import check_series, load_watch
from gridcase.semantic.expr import identifiers, parse_expr
from gridcase.semantic.kpis import (
    Derived,
    Kpi,
    KpiQuery,
    Measure,
    Nested,
    Share,
    load_kpis,
    run_kpi,
)

PER_WORDS = {"asset": "outcome", "wallet": "wallet", "d_market": "market", "condition_id": "market"}


def _formula(kpi: Kpi, kpis: dict[str, Kpi]) -> str:
    if isinstance(kpi, Derived):
        names = identifiers(parse_expr(kpi.expr))
        text = re.sub(
            r"[A-Za-z_]\w*", lambda m: kpis[m[0]].label if m[0] in names else m[0], kpi.expr
        )
        return text.replace(" / ", " ÷ ").replace(" * ", " × ")
    if isinstance(kpi, Share):
        label = kpis[kpi.of].label
        return f"{label} in the slice ÷ {label} overall"
    if isinstance(kpi, Nested):
        return f"Worked out for each {PER_WORDS.get(kpi.per, kpi.per)}, then combined"
    return (
        "Counted once per distinct item"
        if "DISTINCT" in kpi.sql
        else ("Counted over trades" if kpi.sql.startswith("count") else "Added up over trades")
    )


def _definition(kpi: Kpi) -> str:
    if isinstance(kpi, Measure):
        return kpi.sql
    if isinstance(kpi, Derived):
        return kpi.expr
    if isinstance(kpi, Share):
        return f"share of {kpi.of}"
    inner = ", ".join(f"{sql} AS {name}" for name, sql in kpi.inner.items())
    return (
        f"per {kpi.per}: {inner}"
        + (f" WHERE {kpi.where}" if kpi.where != "TRUE" else "")
        + f"; then {kpi.outer}"
    )


def catalog(conn: duckdb.DuckDBPyConnection, as_of: datetime) -> dict[str, Any]:
    kpis, dims = load_kpis()
    watched, params = load_watch()
    overall = {w.kpi: w for w in watched if not w.filters}
    hi = (as_of - timedelta(minutes=params.settle_minutes)).replace(
        minute=0, second=0, microsecond=0
    )
    lo = hi - timedelta(hours=1)
    rows = []
    for kid, kpi in kpis.items():
        if isinstance(kpi, Measure) and kpi.internal:
            continue
        got = run_kpi(conn, KpiQuery(kpi=kid, grain="total", window=(lo, hi), as_of=as_of))
        value = got[0][-1] if got else None
        status = (
            check_series(conn, overall[kid], as_of).output["status"] if kid in overall else None
        )
        rows.append(
            {
                "id": kid,
                "label": kpi.label,
                "description": kpi.description,
                "unit": kpi.unit,
                "formula": _formula(kpi, kpis),
                "definition": _definition(kpi),
                "watched": kid in overall,
                "status": status,
                "value": None if value is None else float(value),
            }
        )
    return {
        "as_of": as_of.isoformat(),
        "hour": [lo.isoformat(), hi.isoformat()],
        "kpis": rows,
        "dimensions": [{"id": d.id, "label": d.label, "values": d.values} for d in dims.values()],
    }
