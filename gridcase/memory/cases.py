"""Cases table, lifecycle state machine, learned priors (Beta-Binomial), similar cases."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import duckdb
from pydantic import BaseModel, Field

from gridcase.explain.schema import Conclusion
from gridcase.ledger.schema import Receipt
from gridcase.ledger.store import read_receipts
from gridcase.planner.hypotheses import Prior, prior_for, season_of
from gridcase.planner.llm import LLMUsage
from gridcase.planner.schema import InvestigationPlan

STATES = [
    "detected",
    "planned",
    "executing",
    "concluded",
    "action_taken",
    "rechecking",
    "closed",
    "reopened",
]
TRANSITIONS: dict[str, set[str]] = {
    "detected": {"planned"},
    "planned": {"executing"},
    "executing": {"concluded"},
    "concluded": {"action_taken", "closed"},
    "action_taken": {"rechecking", "closed"},
    "rechecking": {"closed", "reopened"},
    "reopened": {"planned"},
    "closed": set(),
}


class IllegalTransition(ValueError):
    pass


class CaseRecord(BaseModel):
    case_id: str
    entity: str
    metric: str
    opened_at: datetime
    as_of: datetime
    state: str
    season: str
    top_hypothesis: str | None = None
    plan: InvestigationPlan | None = None
    plan_attempts: int = 0
    conclusion: Conclusion | None = None
    receipts: list[Receipt] = Field(default_factory=list)
    llm_calls: int = 0
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    verdicts: dict[str, str] = Field(default_factory=dict)
    recheck: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    step_errors: dict[str, str] = Field(default_factory=dict)  # step_id -> tool error


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def new_case(
    conn: duckdb.DuckDBPyConnection,
    entity: str,
    metric: str,
    as_of: datetime,
    state: str = "detected",
) -> CaseRecord:
    case = CaseRecord(
        case_id=f"{entity.lower()}-{as_of:%Y%m%dT%H}-{uuid.uuid4().hex[:6]}",
        entity=entity,
        metric=metric,
        opened_at=_now(),
        as_of=as_of,
        state=state,
        season=season_of(as_of.date()),
    )
    save_case(conn, case)
    return case


def save_case(conn: duckdb.DuckDBPyConnection, case: CaseRecord) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO cases (case_id, entity, metric, opened_at, as_of, state, season,
            top_hypothesis, plan_json, conclusion_json, plan_attempts, llm_calls, llm_usage_json,
            verdicts_json, recheck_json, action_json, step_errors_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            case.case_id,
            case.entity,
            case.metric,
            case.opened_at,
            case.as_of,
            case.state,
            case.season,
            case.top_hypothesis,
            case.plan.model_dump_json() if case.plan else None,
            case.conclusion.model_dump_json() if case.conclusion else None,
            case.plan_attempts,
            case.llm_calls,
            case.llm_usage.model_dump_json(),
            json.dumps(case.verdicts),
            json.dumps(case.recheck) if case.recheck else None,
            json.dumps(case.action) if case.action else None,
            json.dumps(case.step_errors),
        ],
    )


def get_case(conn: duckdb.DuckDBPyConnection, case_id: str) -> CaseRecord:
    row = conn.execute(
        "SELECT case_id, entity, metric, opened_at, as_of, state, season, top_hypothesis, "
        "plan_json, conclusion_json, plan_attempts, llm_calls, llm_usage_json, verdicts_json, "
        "recheck_json, action_json, step_errors_json FROM cases WHERE case_id = ?",
        [case_id],
    ).fetchone()
    if row is None:
        raise KeyError(case_id)
    return CaseRecord(
        case_id=row[0],
        entity=row[1],
        metric=row[2],
        opened_at=row[3],
        as_of=row[4],
        state=row[5],
        season=row[6],
        top_hypothesis=row[7],
        plan=InvestigationPlan.model_validate_json(row[8]) if row[8] else None,
        conclusion=Conclusion.model_validate_json(row[9]) if row[9] else None,
        plan_attempts=row[10],
        llm_calls=row[11],
        llm_usage=LLMUsage.model_validate_json(row[12]),
        verdicts=json.loads(row[13] or "{}"),
        recheck=json.loads(row[14]) if row[14] else None,
        action=json.loads(row[15]) if row[15] else None,
        step_errors=json.loads(row[16] or "{}"),
        receipts=read_receipts(conn, case_id),
    )


def advance(conn: duckdb.DuckDBPyConnection, case_id: str, to: str) -> CaseRecord:
    case = get_case(conn, case_id)
    if to not in TRANSITIONS.get(case.state, set()):
        raise IllegalTransition(f"{case.state} -> {to}")
    case.state = to
    save_case(conn, case)
    return case


def list_cases(conn: duckdb.DuckDBPyConnection, state: str | None = None) -> list[CaseRecord]:
    sql = (
        "SELECT case_id FROM cases" + (" WHERE state = ?" if state else "") + " ORDER BY as_of DESC"
    )
    rows = conn.execute(sql, [state] if state else []).fetchall()
    return [get_case(conn, r[0]) for r in rows]


# --------------------------------------------------------------------------- priors


def effective_prior(
    conn: duckdb.DuckDBPyConnection, template_id: str, ba: str, season: str
) -> Prior:
    base = prior_for(template_id, ba, season)
    row = conn.execute(
        "SELECT a_delta, b_delta FROM learned_priors "
        "WHERE template_id = ? AND ba = ? AND season = ?",
        [template_id, ba, season],
    ).fetchone()
    if row is None:
        return base
    return Prior(a=base.a + float(row[0]), b=base.b + float(row[1]))


def update_learned_priors(
    conn: duckdb.DuckDBPyConnection, ba: str, season: str, verdicts: dict[str, str]
) -> None:
    """Beta-Binomial: confirmed -> a+1, refuted -> b+1; other verdicts leave the prior alone."""
    for hid, verdict in verdicts.items():
        da, db_ = (
            (1.0, 0.0)
            if verdict == "confirmed"
            else (0.0, 1.0)
            if verdict == "refuted"
            else (0.0, 0.0)
        )
        if da == db_ == 0.0:
            continue
        conn.execute(
            """
            INSERT INTO learned_priors (template_id, ba, season, a_delta, b_delta)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (template_id, ba, season) DO UPDATE SET
                a_delta = learned_priors.a_delta + excluded.a_delta,
                b_delta = learned_priors.b_delta + excluded.b_delta
            """,
            [hid, ba, season, da, db_],
        )


def close_case(
    conn: duckdb.DuckDBPyConnection, case_id: str, verdicts: dict[str, str]
) -> CaseRecord:
    case = get_case(conn, case_id)
    case.verdicts = verdicts
    case.state = "closed"
    confirmed = [h for h, v in verdicts.items() if v == "confirmed"]
    if confirmed and case.top_hypothesis is None:
        case.top_hypothesis = confirmed[0]
    save_case(conn, case)
    update_learned_priors(conn, case.entity, case.season, verdicts)
    return case


def similar_cases(
    conn: duckdb.DuckDBPyConnection, entity: str, metric: str, season: str, k: int = 3
) -> list[CaseRecord]:
    rows = conn.execute(
        "SELECT case_id FROM cases WHERE state = 'closed' AND entity = ? AND metric = ? "
        "AND season = ? ORDER BY as_of DESC LIMIT ?",
        [entity, metric, season, k],
    ).fetchall()
    return [get_case(conn, r[0]) for r in rows]
