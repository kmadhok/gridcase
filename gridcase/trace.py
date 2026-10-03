"""Small, context-local investigation trace. Store metadata, never prompts or responses."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import duckdb
from pydantic import BaseModel, Field


class Span(BaseModel):
    seq: int
    parent_seq: int | None
    kind: Literal["stage", "tool", "llm"]
    name: str
    started_at: datetime
    ms: float = 0
    status: Literal["ok", "error"] = "ok"
    detail: dict[str, Any] = Field(default_factory=dict)


class Tracer:
    def __init__(self) -> None:
        self.spans: list[Span] = []
        self._parent: int | None = None
        self._started_at = datetime.now(UTC).replace(tzinfo=None)
        self._clock = time.perf_counter()

    def _now(self) -> datetime:
        # Monotonic elapsed time anchored to UTC, so a clock correction cannot invert spans.
        return self._started_at + timedelta(seconds=time.perf_counter() - self._clock)

    @contextmanager
    def span(
        self, kind: Literal["stage", "tool", "llm"], name: str, **detail: Any
    ) -> Iterator[dict[str, Any]]:
        span = Span(
            seq=len(self.spans),
            parent_seq=self._parent,
            kind=kind,
            name=name,
            started_at=self._now(),
            detail=detail,
        )
        self.spans.append(span)
        self._parent = span.seq
        try:
            yield span.detail
        except BaseException as e:
            span.status = "error"
            span.detail["error"] = str(e)
            raise
        finally:
            span.ms = (self._now() - span.started_at).total_seconds() * 1000
            self._parent = span.parent_seq

    def save(self, conn: duckdb.DuckDBPyConnection, case_id: str) -> None:
        if self.spans:
            conn.executemany(
                "INSERT INTO case_trace "
                "(case_id, seq, parent_seq, kind, name, started_at, ms, status, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        case_id,
                        s.seq,
                        s.parent_seq,
                        s.kind,
                        s.name,
                        s.started_at,
                        s.ms,
                        s.status,
                        json.dumps(s.detail, default=str),
                    ]
                    for s in self.spans
                ],
            )


active_tracer: ContextVar[Tracer | None] = ContextVar("active_tracer", default=None)


def read_trace(conn: duckdb.DuckDBPyConnection, case_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT seq, parent_seq, kind, name, started_at, ms, status, detail_json "
        "FROM case_trace WHERE case_id = ? ORDER BY seq",
        [case_id],
    ).fetchall()
    spans = []
    for seq, parent, kind, name, started, ms, status, detail in rows:
        spans.append(
            {
                "seq": seq,
                "parent_seq": parent,
                "kind": kind,
                "name": name,
                "started_at": started.isoformat(),
                "ms": ms,
                "status": status,
                "offset_ms": (started - rows[0][4]).total_seconds() * 1000,
                "detail": json.loads(detail),
            }
        )
    # The free standard playbook goes through the same plan/explain steps without a model;
    # its spans carry no model name and are not model calls.
    models = [s for s in spans if s["kind"] == "llm" and s["detail"].get("model")]
    execute = {s["seq"] for s in spans if s["kind"] == "stage" and s["name"] == "execute"}
    checks = [s for s in spans if s["kind"] == "tool" and s["parent_seq"] in execute]
    return {
        "recorded": bool(spans),
        "spans": spans,
        "totals": {
            "wall_ms": max((s["offset_ms"] + s["ms"] for s in spans), default=0),
            "model_calls": sum(s["detail"].get("attempts", 1) for s in models),
            "input_tokens": sum(s["detail"].get("input_tokens", 0) for s in models),
            "output_tokens": sum(s["detail"].get("output_tokens", 0) for s in models),
            "cost_usd": round(sum(s["detail"].get("cost_usd", 0) for s in models), 6),
            "checks_run": len(checks),
            "checks_failed": sum(s["status"] == "error" for s in checks),
        },
    }
