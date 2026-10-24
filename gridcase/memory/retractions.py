"""Retracted cases: a case judged on hours our record did not have (a collector outage) stays
in the store exactly as it was judged (rule 2), with a retraction beside it saying when and why.

Retractions live in their own table (`case_retractions`), so `save_case` never touches them and
`list_cases` still returns every case: the hourly watch's dedup and its daily paid cap keep
counting retracted cases. The public lists (/api/cases, the Now page, MCP list_open_cases)
leave them out; the case page still serves them with the reason."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
from pydantic import BaseModel

from gridcase.memory.cases import CaseRecord, list_cases

HOUR = timedelta(hours=1)


class Retraction(BaseModel):
    case_id: str
    retracted_at: datetime
    reason: str
    span_from: datetime | None = None  # the gap or outage that justified it, if known
    span_to: datetime | None = None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def retract_case(
    conn: duckdb.DuckDBPyConnection,
    case_id: str,
    reason: str,
    span: tuple[datetime, datetime] | None = None,
    at: datetime | None = None,
) -> Retraction:
    """Retract a case (or replace its retraction). Raises KeyError for an unknown case."""
    if conn.execute("SELECT 1 FROM cases WHERE case_id = ?", [case_id]).fetchone() is None:
        raise KeyError(case_id)
    if not reason.strip():
        raise ValueError("a retraction needs a reason")
    r = Retraction(
        case_id=case_id,
        retracted_at=at or _now(),
        reason=reason.strip(),
        span_from=span[0] if span else None,
        span_to=span[1] if span else None,
    )
    conn.execute(
        "INSERT OR REPLACE INTO case_retractions (case_id, retracted_at, reason, span_from, "
        "span_to) VALUES (?, ?, ?, ?, ?)",
        [r.case_id, r.retracted_at, r.reason, r.span_from, r.span_to],
    )
    return r


def unretract_case(conn: duckdb.DuckDBPyConnection, case_id: str) -> bool:
    """Remove a case's retraction; False if it had none."""
    had = get_retraction(conn, case_id) is not None
    conn.execute("DELETE FROM case_retractions WHERE case_id = ?", [case_id])
    return had


def get_retraction(conn: duckdb.DuckDBPyConnection, case_id: str) -> Retraction | None:
    return retractions(conn, [case_id]).get(case_id)


def retractions(
    conn: duckdb.DuckDBPyConnection, case_ids: list[str] | None = None
) -> dict[str, Retraction]:
    """Every retraction (or those of `case_ids`), by case id."""
    sql = "SELECT case_id, retracted_at, reason, span_from, span_to FROM case_retractions"
    params: list[object] = []
    if case_ids is not None:
        if not case_ids:
            return {}
        sql += f" WHERE case_id IN ({', '.join('?' * len(case_ids))})"
        params = list(case_ids)
    try:
        rows = conn.execute(sql, params).fetchall()
    except duckdb.CatalogException:  # a store opened read-only from before this table
        return {}
    return {
        row[0]: Retraction(
            case_id=row[0], retracted_at=row[1], reason=row[2], span_from=row[3], span_to=row[4]
        )
        for row in rows
    }


def public_cases(
    conn: duckdb.DuckDBPyConnection, include_retracted: bool = False
) -> list[CaseRecord]:
    """`list_cases` for the public lists: newest first, retracted cases left out by default."""
    cases = list_cases(conn)
    if include_retracted:
        return cases
    hidden = retractions(conn)
    return [c for c in cases if c.case_id not in hidden]


# --------------------------------------------------------------------------- outage matching


def case_hours(case: CaseRecord) -> tuple[datetime, datetime] | None:
    """The hours a case investigated: its first kpi.compare window."""
    compare = next((r for r in case.receipts if r.tool == "kpi.compare"), None)
    w = compare.inputs.get("window") if compare else None
    return (datetime.fromisoformat(w[0]), datetime.fromisoformat(w[1])) if w else None


class OutageMatch(BaseModel):
    case_id: str
    entity: str
    metric: str
    opened_at: datetime
    window: tuple[datetime, datetime]
    why: str  # "collector gap" or "empty then, filled since"
    span: tuple[datetime, datetime]
    retracted: bool  # already retracted before this run


def outage_cases(
    conn: duckdb.DuckDBPyConnection, since: datetime, until: datetime
) -> list[OutageMatch]:
    """Cases whose investigated hours overlap [since, until) and either overlap a recorded
    collector gap, or include an hour that had no trades at all when the case was judged
    (trades_as_of its as_of) and has trades now (a gap filled later). Read-only.

    An empty hour is checked across the whole feed, not the case's segment: a segment can
    truly have no trades in an hour, the whole of Polymarket cannot."""
    gaps = conn.execute(
        "SELECT gap_from, gap_to FROM collector_gaps WHERE gap_from < ? AND gap_to > ? "
        "ORDER BY gap_from",
        [until, since],
    ).fetchall()
    done = retractions(conn)
    out: list[OutageMatch] = []
    for case in reversed(list_cases(conn)):  # oldest first
        w = case_hours(case)
        if w is None or not (w[0] < until and w[1] > since):
            continue
        hit = [(g0, g1) for g0, g1 in gaps if g0 < w[1] and g1 > w[0]]
        if hit:
            span = (max(min(g[0] for g in hit), w[0]), min(max(g[1] for g in hit), w[1]))
            why = "collector gap"
        else:
            filled = [
                h
                for h, then, now in conn.execute(
                    "SELECT time_bucket(INTERVAL 1 HOUR, ts) AS h, "
                    "count(*) FILTER (WHERE collected_at <= ?), count(*) FROM trades "
                    "WHERE ts >= ? AND ts < ? GROUP BY h ORDER BY h",
                    [case.as_of, w[0], w[1]],
                ).fetchall()
                if then == 0 and now > 0
            ]
            if not filled:
                continue
            span, why = (filled[0], filled[-1] + HOUR), "empty then, filled since"
        out.append(
            OutageMatch(
                case_id=case.case_id,
                entity=case.entity,
                metric=case.metric,
                opened_at=case.opened_at,
                window=w,
                why=why,
                span=span,
                retracted=case.case_id in done,
            )
        )
    return out
