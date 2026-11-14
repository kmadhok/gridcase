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


def _fingerprint(
    conn: duckdb.DuckDBPyConnection, hi: datetime, as_of: datetime
) -> tuple[Any, ...] | None:
    # Match compile_kpi's second-precision TIMESTAMP literals. Trades are immutable and
    # append-only: count catches added visible rows, including earlier first-trade history.
    # Max also catches an earlier replay cutoff (any excluded row was collected after it),
    # even if new backdated rows leave count unchanged. Trades >= hi cannot lower a window
    # member's first-trade time.
    # Markets can be replaced without advancing fetched_at, so every row's joined fields are
    # hashed as ONE JSON string and the row hashes XORed. DuckDB's multi-argument hash(a, b, c)
    # is not safe here: swapping one column's values between rows cancels in the XOR. An exact
    # snapshot is safe too but is tens of MB on the live store, every five minutes.
    return conn.execute(
        """SELECT count(*), max(collected_at),
                  (SELECT count(*) FROM markets),
                  (SELECT bit_xor(hash(to_json([condition_id, category, event_title])))
                   FROM markets)
           FROM trades_as_of(?) WHERE ts < ?""",
        [as_of.replace(microsecond=0), hi],
    ).fetchone()


def _hour_values(
    conn: duckdb.DuckDBPyConnection,
    kids: list[str],
    lo: datetime,
    hi: datetime,
    as_of: datetime,
) -> dict[str, float | None]:
    """Each KPI's total over [lo, hi) at as_of, reused while the fingerprint is unchanged.
    The fingerprint is read before and after computing; values are kept only when both match,
    so a trade or market loaded by another cursor meanwhile is never stored under inputs that
    don't describe it (trades only grow, so a load always moves the fingerprint)."""
    global _value_cache
    store = _store(conn)
    # A replaced file is a new store. Memory databases share names and inodes, so they are
    # told apart by the connection and the database selected on it.
    path = Path(store)
    if path.is_file():
        stat = path.stat()
        identity: Any = (store, stat.st_dev, stat.st_ino)
    else:
        identity = (store, conn)
    key = (identity, lo, hi, _fingerprint(conn, hi, as_of))
    cached = _value_cache
    if cached is not None and cached[0] == key and all(k in cached[1] for k in kids):
        return dict(cached[1])
    values = {k: _compute(conn, k, lo, hi, as_of) for k in kids}
    if (identity, lo, hi, _fingerprint(conn, hi, as_of)) == key:
        _value_cache = (key, values)
    return dict(values)


def _compute(
    conn: duckdb.DuckDBPyConnection, kid: str, lo: datetime, hi: datetime, as_of: datetime
) -> float | None:
    got = run_kpi(conn, KpiQuery(kpi=kid, grain="total", window=(lo, hi), as_of=as_of))
    value = got[0][-1] if got else None
    return None if value is None else float(value)


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
    values = _hour_values(conn, [k for k, v in kpis.items() if not is_internal(v)], lo, hi, as_of)
    rows = []
    for kid, kpi in kpis.items():
        if is_internal(kpi):
            continue
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
