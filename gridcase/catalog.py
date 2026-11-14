"""The KPI catalog: every KPI a reader may meet, in plain words, with its governed definition,
whether it is watched, and its value over the last complete hour."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from gridcase.market_alarm import _store, check_series, load_watch
from gridcase.semantic.expr import identifiers, parse_expr
from gridcase.semantic.kpis import (
    Derived,
    Kpi,
    KpiQuery,
    Measure,
    Nested,
    Share,
    is_internal,
    load_kpis,
    run_kpi,
)

PER_WORDS = {"asset": "outcome", "wallet": "wallet", "d_market": "market", "condition_id": "market"}

# Only one store/window/input generation is retained. Keep the values separate from the
# payload: definitions, as_of and (especially) alarm status are still rebuilt on every call.
_value_cache: tuple[tuple[Any, ...], dict[str, float | None]] | None = None


def clear_value_cache() -> None:
    global _value_cache
    _value_cache = None


def _hour_values(
    conn: duckdb.DuckDBPyConnection, lo: datetime, hi: datetime, as_of: datetime
) -> dict[str, float | None]:
    global _value_cache
    store = _store(conn)
    # Memory databases all have the same name. A replaced file is a new store too.
    path = Path(store)
    if path.is_file():
        stat = path.stat()
        identity: Any = (store, stat.st_dev, stat.st_ino)
    else:
        identity = conn
    # Match compile_kpi's second-precision TIMESTAMP literals. Trades are immutable and
    # append-only: count catches added visible rows, including earlier first-trade history.
    # Max also catches an earlier replay cutoff (any excluded row was collected after it),
    # even if new backdated rows leave count unchanged. Trades >= hi cannot lower a window
    # member's first-trade time.
    # Markets can be replaced without advancing fetched_at, so hash the joined fields of every
    # row (count + XOR of 64-bit row hashes). An exact snapshot was correct but is tens of MB on
    # the live store (hundreds of thousands of markets), every five minutes, under the memory cap.
    fingerprint = conn.execute(
        """SELECT count(*), max(collected_at),
                  (SELECT count(*) FROM markets),
                  (SELECT bit_xor(hash(condition_id, category, event_title)) FROM markets)
           FROM trades_as_of(?) WHERE ts < ?""",
        [as_of.replace(microsecond=0), hi],
    ).fetchone()
    key = (identity, lo, hi, fingerprint)
    cached = _value_cache
    if cached is None or cached[0] != key:
        cached = (key, {})
        _value_cache = cached
    # Keep this generation local so concurrent callers cannot put values under another
    # caller's key. The server's one writer loads trades before refreshing either page.
    return cached[1]


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
    values = _hour_values(conn, lo, hi, as_of)
    rows = []
    for kid, kpi in kpis.items():
        if is_internal(kpi):
            continue
        if kid not in values:
            got = run_kpi(conn, KpiQuery(kpi=kid, grain="total", window=(lo, hi), as_of=as_of))
            value = got[0][-1] if got else None
            values[kid] = None if value is None else float(value)
        value = values[kid]
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
        "dimensions": [
            {"id": d.id, "label": d.label, "description": d.description, "values": d.values}
            for d in dims.values()
        ],
    }
