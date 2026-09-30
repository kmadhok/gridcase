"""The MarketCase KPI alarm: plain statistics on each watched series (ontology/watch.yaml).

Each of the latest complete hours is compared with the same hour of the day on previous days
(log scale for volumes and counts). An alarm is `min_run` hours in a row beyond `k_sigma` on
the same side, or one hour beyond `single_hour_sigma`. Hours the collector missed are never
judged or used as history; with too little history the status is "learning". The result is a
receipt (tool "kpi.alarm") carrying the compiled KPI query, leak-free at its as_of.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from typing import Any

import duckdb
import yaml
from pydantic import BaseModel, Field

from gridcase.config import ONTOLOGY_DIR
from gridcase.ledger.schema import Receipt
from gridcase.semantic.kpis import (
    FilterValue,
    KpiQuery,
    Measure,
    Nested,
    compile_kpi,
    load_kpis,
    segment_label,
)
from gridcase.tools.base import rows_hash

HOUR = timedelta(hours=1)


class WatchedSeries(BaseModel):
    kpi: str
    filters: dict[str, FilterValue] = Field(default_factory=dict)

    @property
    def segment(self) -> str:
        """The case's `entity`: 'all' or 'category=Sports'."""
        return segment_label(self.filters)

    @property
    def label(self) -> str:
        return self.kpi if not self.filters else f"{self.kpi} · {self.segment}"


class WatchParams(BaseModel):
    lookback_days: int = 28
    min_history_days: int = 3
    recent_hours: int = 6
    settle_minutes: int = 15
    k_sigma: float = 3.0
    min_run: int = 2
    single_hour_sigma: float = 5.0
    log_scale: list[str] = Field(default_factory=list)
    min_sd: dict[str, float] = Field(default_factory=lambda: {"log": 0.05, "linear": 0.01})
    warmup_days: dict[str, int] = Field(default_factory=dict)


@cache
def load_watch() -> tuple[list[WatchedSeries], WatchParams]:
    doc = yaml.safe_load((ONTOLOGY_DIR / "watch.yaml").read_text())
    return [WatchedSeries(**w) for w in doc["watched"]], WatchParams(**doc["params"])


def _gap_hours(conn: duckdb.DuckDBPyConnection, lo: datetime, hi: datetime) -> set[datetime]:
    out: set[datetime] = set()
    for a, b in conn.execute(
        "SELECT gap_from, gap_to FROM collector_gaps WHERE gap_to > ? AND gap_from < ?", [lo, hi]
    ).fetchall():
        h = a.replace(minute=0, second=0, microsecond=0)
        while h < b:
            out.add(h)
            h += HOUR
    return out


# Settled hours never change: the collector sees a trade within ~15 minutes and the server
# loads every 5, so an hour that ended SETTLED_AFTER ago has all its trades. Their values are
# kept per store and series, and only newer hours are queried. Keys include the store's file.
SETTLED_AFTER = timedelta(hours=1)
_MAX_CACHED = 200_000
_cache: dict[tuple[str, str, str, datetime], float | None] = {}


def clear_cache() -> None:
    _cache.clear()


def _store(conn: duckdb.DuckDBPyConnection) -> str:
    row = conn.execute(
        "SELECT coalesce(path, database_name) FROM duckdb_databases() "
        "WHERE database_name = current_database()"
    ).fetchone()
    return str(row[0]) if row else ""


def _hour_values(
    conn: duckdb.DuckDBPyConnection, q: KpiQuery, series: WatchedSeries, as_of: datetime
) -> dict[datetime, float]:
    lo, hi = q.window
    key = (_store(conn), series.kpi, series.segment)

    def settled(h: datetime) -> bool:
        return h + HOUR + SETTLED_AFTER <= as_of

    hours = [lo + i * HOUR for i in range(int((hi - lo) / HOUR))]
    first_missing = next((h for h in hours if not (settled(h) and (*key, h) in _cache)), hi)
    out = {h: _cache[(*key, h)] for h in hours if h < first_missing}
    if first_missing < hi:
        fresh_q = q.model_copy(update={"window": (first_missing, hi)})
        fresh = {t: v for t, v in conn.execute(compile_kpi(fresh_q).sql).fetchall()}
        if len(_cache) > _MAX_CACHED:
            _cache.clear()
        for h in hours:
            if h >= first_missing:
                v = fresh.get(h)
                out[h] = None if v is None else float(v)
                if settled(h):
                    _cache[(*key, h)] = out[h]
    return {h: v for h, v in out.items() if v is not None}


@dataclass
class Hourly:
    """Each complete hour's value next to its usual level for that hour of the day."""

    rows: list[dict[str, Any]]  # t, v, expected, low, high, z (None when not judgeable)
    sql: str
    raw: list[tuple[Any, ...]]
    history_days: int
    gap_hours_skipped: int
    judged_from: datetime
    judged_until: datetime
    log_scale: bool


def hourly(
    conn: duckdb.DuckDBPyConnection,
    series: WatchedSeries,
    as_of: datetime,
    p: WatchParams,
    hours: int,
) -> Hourly:
    kpis, _ = load_kpis()
    kpi = kpis[series.kpi]
    log_scale = series.kpi in p.log_scale
    # no trades in an hour means 0, not unknown
    zero_fill = isinstance(kpi, Measure) or (isinstance(kpi, Nested) and kpi.empty_is_zero)

    judged_until = (as_of - timedelta(minutes=p.settle_minutes)).replace(
        minute=0, second=0, microsecond=0
    )
    judged_from = judged_until - timedelta(hours=hours)
    start_row = conn.execute("SELECT min(ts) FROM trades_as_of(?)", [as_of]).fetchone()
    first = start_row[0] if start_row else None
    record_start = (
        (
            first
            if first == first.replace(minute=0, second=0, microsecond=0)
            else first.replace(minute=0, second=0, microsecond=0) + HOUR
        )
        if first is not None
        else judged_until
    )
    lo = max(record_start, judged_from - timedelta(days=p.lookback_days))
    hi = max(judged_until, lo + HOUR)
    q = KpiQuery(kpi=series.kpi, grain="hour", window=(lo, hi), as_of=as_of, filters=series.filters)
    compiled = compile_kpi(q)  # the audit query: re-running it reproduces every hour below
    values = _hour_values(conn, q, series, as_of)
    raw = sorted(values.items())
    gaps = _gap_hours(conn, lo, judged_until)

    def value_at(h: datetime) -> float | None:
        if h in gaps or h < record_start:
            return None
        v = values.get(h)
        return 0.0 if v is None and zero_fill else v

    def scale(v: float) -> float:
        return math.log1p(max(v, 0.0)) if log_scale else v

    def unscale(x: float) -> float:
        return math.expm1(x) if log_scale else x

    floor = p.min_sd["log" if log_scale else "linear"]
    warm = (judged_until - record_start).days >= p.warmup_days.get(series.kpi, 0)
    rows: list[dict[str, Any]] = []
    history_days = 0
    skipped = 0
    h = judged_from
    while h < judged_until:
        v = value_at(h)
        if h in gaps:
            skipped += 1
        past = [value_at(h - timedelta(days=d)) for d in range(1, p.lookback_days + 1)]
        hist = [scale(x) for x in past if x is not None]
        if h >= record_start:
            history_days = max(history_days, len(hist))
        row: dict[str, Any] = {
            "t": h,
            "v": v,
            "expected": None,
            "low": None,
            "high": None,
            "z": None,
        }
        if len(hist) >= p.min_history_days and warm:
            mean = sum(hist) / len(hist)
            sd = max(math.sqrt(sum((x - mean) ** 2 for x in hist) / len(hist)), floor)
            row["expected"] = unscale(mean)
            row["low"] = (
                max(unscale(mean - p.k_sigma * sd), 0.0) if log_scale else mean - p.k_sigma * sd
            )
            row["high"] = unscale(mean + p.k_sigma * sd)
            if v is not None:
                row["z"] = (scale(v) - mean) / sd
        rows.append(row)
        h += HOUR
    return Hourly(
        rows, compiled.sql, raw, history_days, skipped, judged_from, judged_until, log_scale
    )


def check_series(
    conn: duckdb.DuckDBPyConnection,
    series: WatchedSeries,
    as_of: datetime,
    params: WatchParams | None = None,
) -> Receipt:
    t0 = time.perf_counter()
    p = params or load_watch()[1]
    hv = hourly(conn, series, as_of, p, p.recent_hours)
    judged = [r for r in hv.rows if r["z"] is not None]
    history_days, skipped = hv.history_days, hv.gap_hours_skipped
    judged_from, judged_until, log_scale = hv.judged_from, hv.judged_until, hv.log_scale
    compiled_sql, rows = hv.sql, hv.raw

    run: list[dict[str, Any]] = []
    best: list[dict[str, Any]] = []
    for j in judged:
        extreme = abs(j["z"]) > p.k_sigma
        continues = (
            run and extreme and j["t"] - run[-1]["t"] == HOUR and (j["z"] > 0) == (run[-1]["z"] > 0)
        )
        run = [*run, j] if continues else ([j] if extreme else [])
        if run and (len(run) >= p.min_run or any(abs(x["z"]) > p.single_hour_sigma for x in run)):
            best = list(run)
    status = "alarm" if best else ("normal" if judged else "learning")
    focus = best or judged
    worst = max(focus, key=lambda j: abs(j["z"]), default=None)

    def r6(x: float | None) -> float | None:
        return None if x is None else round(float(x), 6)

    output: dict[str, Any] = {
        "series": series.label,
        "kpi": series.kpi,
        "filters": series.filters,
        "status": status,
        "direction": ("up" if best[0]["z"] > 0 else "down") if best else None,
        "run_start": best[0]["t"].isoformat() if best else None,
        "run_end": best[-1]["t"].isoformat() if best else None,
        "run_hours": len(best),
        "worst_hour": worst["t"].isoformat() if worst else None,
        "value": r6(worst["v"]) if worst else None,
        "expected": r6(worst["expected"]) if worst else None,
        "ratio": r6(worst["v"] / worst["expected"]) if worst and worst["expected"] else None,
        "z": r6(worst["z"]) if worst else None,
        "k_sigma": p.k_sigma,
        "history_days": history_days,
        "hours_judged": len(judged),
        "gap_hours_skipped": skipped,
        "judged_from": judged_from.isoformat(),
        "judged_until": judged_until.isoformat(),
        "log_scale": log_scale,
    }
    return Receipt.build(
        tool="kpi.alarm",
        inputs={
            "kpi": series.kpi,
            "filters": series.filters,
            **p.model_dump(exclude={"log_scale", "warmup_days", "min_sd"}),
        },
        as_of=as_of,
        output=output,
        data_version=None,
        effect_size=output["z"],
        runtime_ms=int((time.perf_counter() - t0) * 1000),
        sql=[{"sql": compiled_sql, "rows_hash": rows_hash(rows)}],
    )


def series_view(
    conn: duckdb.DuckDBPyConnection, series: WatchedSeries, as_of: datetime, hours: int = 48
) -> dict[str, Any]:
    """What the Now page draws: the last `hours` complete hours against their usual band, and
    the alarm's verdict on the latest ones."""
    p = load_watch()[1]
    kpis, _ = load_kpis()
    verdict = check_series(conn, series, as_of, p).output
    hv = hourly(conn, series, as_of, p, hours)
    latest = next((r for r in reversed(hv.rows) if r["v"] is not None), None)
    return {
        "series": series.label,
        "kpi": series.kpi,
        "label": kpis[series.kpi].label,
        "unit": kpis[series.kpi].unit,
        "segment": series.segment,
        "filters": series.filters,
        "status": verdict["status"],
        "direction": verdict["direction"],
        "ratio": verdict["ratio"],
        "history_days": verdict["history_days"],
        "latest": None
        if latest is None
        else {"t": latest["t"].isoformat(), "value": latest["v"], "usual": latest["expected"]},
        "points": [
            {
                "t": r["t"].isoformat(),
                "value": r["v"],
                "usual": r["expected"],
                "low": r["low"],
                "high": r["high"],
            }
            for r in hv.rows
        ],
    }
