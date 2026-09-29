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
from datetime import datetime, timedelta
from functools import cache
from typing import Any

import duckdb
import yaml
from pydantic import BaseModel, Field

from gridcase.config import ONTOLOGY_DIR
from gridcase.ledger.schema import Receipt
from gridcase.semantic.kpis import KpiQuery, Measure, compile_kpi, load_kpis, segment_label
from gridcase.tools.base import rows_hash

HOUR = timedelta(hours=1)


class WatchedSeries(BaseModel):
    kpi: str
    filters: dict[str, str] = Field(default_factory=dict)

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


def check_series(
    conn: duckdb.DuckDBPyConnection,
    series: WatchedSeries,
    as_of: datetime,
    params: WatchParams | None = None,
) -> Receipt:
    t0 = time.perf_counter()
    p = params or load_watch()[1]
    kpis, _ = load_kpis()
    kpi = kpis[series.kpi]
    log_scale = series.kpi in p.log_scale
    zero_fill = isinstance(kpi, Measure)  # no trades in an hour means 0, not unknown

    judged_until = (as_of - timedelta(minutes=p.settle_minutes)).replace(
        minute=0, second=0, microsecond=0
    )
    judged_from = judged_until - timedelta(hours=p.recent_hours)
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
    q = KpiQuery(
        kpi=series.kpi,
        grain="hour",
        window=(lo, max(judged_until, lo + HOUR)),
        as_of=as_of,
        filters=series.filters,
    )
    compiled = compile_kpi(q)
    rows = [tuple(r) for r in conn.execute(compiled.sql).fetchall()]
    values: dict[datetime, float] = {t: float(v) for t, v in rows if v is not None}
    gaps = _gap_hours(conn, lo, judged_until)

    def value_at(h: datetime) -> float | None:
        if h in gaps or h < record_start:
            return None
        v = values.get(h)
        return 0.0 if v is None and zero_fill else v

    def scale(v: float) -> float:
        return math.log1p(max(v, 0.0)) if log_scale else v

    floor = p.min_sd["log" if log_scale else "linear"]
    warm = (judged_until - record_start).days >= p.warmup_days.get(series.kpi, 0)
    judged: list[dict[str, Any]] = []
    history_days = 0
    skipped = 0
    h = max(judged_from, record_start)
    while h < judged_until:
        v = value_at(h)
        if h in gaps:
            skipped += 1
        past = [value_at(h - timedelta(days=d)) for d in range(1, p.lookback_days + 1)]
        hist = [scale(x) for x in past if x is not None]
        history_days = max(history_days, len(hist))
        if v is not None and len(hist) >= p.min_history_days and warm:
            mean = sum(hist) / len(hist)
            sd = max(math.sqrt(sum((x - mean) ** 2 for x in hist) / len(hist)), floor)
            expected = math.expm1(mean) if log_scale else mean
            judged.append({"t": h, "v": v, "z": (scale(v) - mean) / sd, "expected": expected})
        h += HOUR

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
        sql=[{"sql": compiled.sql, "rows_hash": rows_hash(rows)}],
    )
