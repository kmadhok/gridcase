"""The GridCase service.

* MCP (2026-07-28 stateless Streamable HTTP at /mcp): `list_open_cases`, `get_case`,
  `investigate` (returns a task handle; needs the API token), `get_task`, every Phase 3 tool
  read-only as `tool_<name>`, and `bench_summary`. Nothing that mutates the ontology.
* JSON API for the UI at /api/*, read-only, no auth.
* The built UI (web/dist) as static files at /.

One DuckDB connection is shared through cursors: DuckDB allows one process per file, so
the service also runs the hourly ingest and the re-check drain in background threads when
configured (see DECISIONS, Phase 6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server import MCPServer
from pydantic import BaseModel, Field
from starlette.routing import Route

from gridcase import config
from gridcase.ask.context import bench_context, case_context
from gridcase.ask.engine import Asker, answer_question, case_checker
from gridcase.ask.llm import default_asker_factory
from gridcase.bench.run import SuiteResult
from gridcase.bench.score import score
from gridcase.investigate import investigate
from gridcase.memory.cases import CaseRecord, get_case, list_cases
from gridcase.planner.llm import LLM, AnthropicLLM, FakeLLM
from gridcase.server.scheduler import drain_scheduled
from gridcase.server.tasks import TaskRegistry
from gridcase.store.db import connect
from gridcase.tools.base import run_tool
from gridcase.tools.registry import TOOL_INPUTS, TOOL_NAMES
from gridcase.watch import watch as run_watch

log = logging.getLogger("gridcase")
WEB_DIST = config.ROOT / "web" / "dist"
SETTLE_WINDOW = timedelta(hours=3)  # a forecast revised within this window is not settled yet
# EIA's US48 forecast is a computed sum that keeps filling in for about a day; reporting BAs
# settle within hours.
SETTLE_AGE = {"US48": timedelta(hours=24)}


def _parse(t: str) -> datetime:
    return (
        datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        if "+" in t or t.endswith("Z")
        else datetime.fromisoformat(t)
    )


def _case_summary(c: CaseRecord) -> dict[str, Any]:
    return {
        "case_id": c.case_id,
        "entity": c.entity,
        "metric": c.metric,
        "as_of": c.as_of.isoformat(),
        "opened_at": c.opened_at.isoformat(),
        "state": c.state,
        "season": c.season,
        "top_hypothesis": c.top_hypothesis,
        "llm_calls": c.llm_calls,
        "cost_usd": c.llm_usage.cost_usd,
    }


def _case_full(c: CaseRecord) -> dict[str, Any]:
    return {
        **_case_summary(c),
        "plan": c.plan.model_dump(mode="json") if c.plan else None,
        "plan_attempts": c.plan_attempts,
        "conclusion": c.conclusion.model_dump(mode="json") if c.conclusion else None,
        "receipts": [r.model_dump(mode="json") for r in c.receipts],
        "verdicts": c.verdicts,
        "recheck": c.recheck,
        "step_errors": c.step_errors,
        "action": {k: v for k, v in (c.action or {}).items() if k != "path"},
    }


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    case_id: str | None = None
    page: Literal["bench"] | None = None


class AskLimiter:
    """Per-visitor sliding window plus a daily spending cap, in memory (reset on restart)."""

    def __init__(self, per_window: int, window_s: float, daily_usd: float) -> None:
        self.per_window, self.window_s, self.daily_usd = per_window, window_s, daily_usd
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.spent: dict[str, float] = defaultdict(float)
        self.lock = threading.Lock()

    def admit(self, visitor: str) -> None:
        now = time.monotonic()
        today = datetime.now(UTC).date().isoformat()
        with self.lock:
            if self.spent[today] >= self.daily_usd:
                raise HTTPException(
                    429, "The question box has used today's budget; try again tomorrow."
                )
            q = self.hits[visitor]
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.per_window:
                raise HTTPException(429, "Too many questions; please wait a few minutes.")
            q.append(now)

    def charge(self, usd: float) -> None:
        with self.lock:
            self.spent[datetime.now(UTC).date().isoformat()] += usd


def create_app(
    db_path: Path | None = None,
    token: str | None = None,
    llm_factory: Callable[[], LLM] | None = None,
    drain_interval_s: float | None = 600.0,
    ingest_interval_s: float | None = None,
    watch: bool = False,
    asker_factory: Callable[[], Asker] | None = None,
    ask_per_window: int = 10,
    ask_window_s: float = 600.0,
    ask_daily_usd: float | None = None,
) -> FastAPI:
    db_path = db_path or config.DB_PATH
    token = token if token is not None else config.api_token()
    llm_factory = llm_factory or AnthropicLLM
    asker_factory = asker_factory or default_asker_factory()
    limiter = AskLimiter(
        ask_per_window,
        ask_window_s,
        config.ask_daily_usd() if ask_daily_usd is None else ask_daily_usd,
    )
    conn = connect(db_path)
    registry = TaskRegistry()
    stop = threading.Event()

    def cur() -> duckdb.DuckDBPyConnection:
        return conn.cursor()

    mcp = MCPServer(
        "gridcase",
        instructions=(
            "Governed investigation engine for plan-vs-actual misses on the US grid. Read cases, "
            "run the deterministic tools at an as_of, or start an investigation (token required)."
        ),
    )

    @mcp.tool()
    def list_open_cases() -> dict[str, Any]:
        """Cases that are not closed, newest first."""
        c = cur()
        try:
            return {"cases": [_case_summary(x) for x in list_cases(c) if x.state != "closed"]}
        finally:
            c.close()

    @mcp.tool(name="get_case")
    def get_case_mcp(case_id: str) -> dict[str, Any]:
        """A case with its plan, conclusion, receipts (with SQL) and re-check."""
        c = cur()
        try:
            return _case_full(get_case(c, case_id))
        except KeyError as e:
            raise ValueError(f"unknown case {case_id}") from e
        finally:
            c.close()

    @mcp.tool(name="investigate")
    def investigate_mcp(entity: str, metric: str, as_of: str, token: str) -> dict[str, Any]:
        """Start an investigation and return a task handle to poll with get_task.
        Requires the API token."""
        if not token or token != app.state.token:
            raise ValueError("unauthorized: bad or missing token")
        when = _parse(as_of)

        def job() -> dict[str, Any]:
            c = cur()
            try:
                case = investigate(c, llm_factory(), entity=entity, metric=metric, as_of=when)
            finally:
                c.close()
            return {
                "case_id": case.case_id,
                "state": case.state,
                "top_hypothesis": case.top_hypothesis,
            }

        return {"task_id": registry.submit(job)}

    @mcp.tool()
    def get_task(task_id: str) -> dict[str, Any]:
        """Poll a task handle: status queued|running|done|failed, with result or error."""
        try:
            t = registry.get(task_id)
        except KeyError as e:
            raise ValueError(f"unknown task {task_id}") from e
        out = {"task_id": task_id, "status": t["status"], "error": t["error"]}
        if t["result"]:
            out.update(t["result"])
        return out

    def _make_tool(name: str) -> None:
        model = TOOL_INPUTS[name]

        def fn(inputs: dict[str, Any], as_of: str) -> dict[str, Any]:
            c = cur()
            try:
                r = run_tool(c, name, model.model_validate(inputs), _parse(as_of))
            finally:
                c.close()
            return r.model_dump(mode="json")

        fn.__name__ = "tool_" + name.replace(".", "_")
        fn.__doc__ = (
            f"Run {name} read-only at as_of and return its receipt (inputs: {model.__name__})."
        )
        mcp.tool(name=fn.__name__)(fn)

    for tool_name in TOOL_NAMES:
        _make_tool(tool_name)

    @mcp.tool()
    def bench_summary() -> dict[str, Any]:
        """The latest benchmark run's summary from bench/results."""
        runs = _bench_runs()
        return runs[-1] if runs else {"runs": 0}

    # ------------------------------------------------------------------ background work

    def background() -> None:
        next_ingest = time.monotonic()
        next_drain = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            if ingest_interval_s and now >= next_ingest:
                try:
                    _live_ingest(cur())
                except Exception as e:  # noqa: BLE001
                    log.warning("live ingest failed: %s", e)
                if watch:  # fresh data in: does any region's KPI alarm fire?
                    c = cur()
                    try:
                        run_watch(
                            c,
                            datetime.now(UTC).replace(tzinfo=None),
                            llm_factory,
                            FakeLLM,
                            config.auto_paid_cases_per_day(),
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("watch failed: %s", e)
                    finally:
                        c.close()
                next_ingest = now + ingest_interval_s
            if drain_interval_s and now >= next_drain:
                try:
                    drain_scheduled(cur())
                except Exception as e:  # noqa: BLE001
                    log.warning("drain failed: %s", e)
                next_drain = now + drain_interval_s
            stop.wait(5.0)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        worker = None
        if drain_interval_s or ingest_interval_s:
            worker = threading.Thread(target=background, daemon=True, name="gridcase-bg")
            worker.start()
        async with mcp.session_manager.run():
            yield
        stop.set()

    mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    app = FastAPI(title="GridCase", lifespan=lifespan)
    app.state.mcp = mcp
    app.state.tasks = registry
    app.state.token = token
    app.state.conn = conn

    # ------------------------------------------------------------------ JSON API

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "time": datetime.now(UTC).isoformat(timespec="seconds")}

    @app.get("/api/cases")
    def api_cases() -> list[dict[str, Any]]:
        c = cur()
        try:
            return [_case_summary(x) for x in list_cases(c)]
        finally:
            c.close()

    @app.get("/api/cases/{case_id}")
    def api_case(case_id: str) -> dict[str, Any]:
        c = cur()
        try:
            return _case_full(get_case(c, case_id))
        except KeyError as e:
            raise HTTPException(404, f"unknown case {case_id}") from e
        finally:
            c.close()

    @app.post("/api/ask")
    def api_ask(req: AskRequest, request: Request) -> dict[str, Any]:
        """Evidence-only answers (hard rule 6): about a case, with read-only checks at its
        as_of, or about the scorecard. Every sentence is verified like the conclusion."""
        if not req.case_id and req.page != "bench":
            raise HTTPException(422, "ask about an investigation (case_id) or the scorecard")
        c = cur()
        try:
            if req.case_id:
                try:
                    case = get_case(c, req.case_id)
                except KeyError as e:
                    raise HTTPException(404, f"unknown case {req.case_id}") from e
                limiter.admit(request.client.host if request.client else "?")
                res = answer_question(
                    asker_factory(),
                    req.question,
                    context=case_context(case),
                    receipts={r.receipt_id: r for r in case.receipts},
                    run_check=case_checker(c, case),
                )
            else:
                limiter.admit(request.client.host if request.client else "?")
                ctx, receipts = bench_context(_bench_runs())
                res = answer_question(
                    asker_factory(), req.question, context=ctx, receipts=receipts, run_check=None
                )
        finally:
            c.close()
        limiter.charge(res.usage.cost_usd)
        return {
            "sentences": [cl.model_dump() for cl in res.sentences],
            "refused": res.refused,
            "new_receipts": [r.model_dump(mode="json") for r in res.new_receipts],
            "dropped": res.dropped,
            "cost_usd": round(res.usage.cost_usd, 4),
        }

    @app.get("/api/map")
    def api_map() -> dict[str, Any]:
        c = cur()
        try:
            regions = []
            cases = list_cases(c)
            now = datetime.now(UTC).replace(tzinfo=None)
            for ba in config.REGIONS:
                # EIA publishes aggregate forecasts incrementally (a partial US48 sum can sit
                # at 40% for hours), so an hour whose forecast moved by more than 5% in the last
                # SETTLE_HOURS is skipped: the tile shows the latest *settled* hour.
                row = c.execute(
                    """
                    WITH d AS (SELECT valid_time, value FROM as_of(TIMESTAMP '2100-01-01')
                               WHERE series_id = 'eia.D' AND entity_id = ?),
                         f AS (SELECT valid_time, value FROM as_of(TIMESTAMP '2100-01-01')
                               WHERE series_id = 'eia.DF' AND entity_id = ?),
                         unsettled AS (
                            SELECT DISTINCT o.valid_time
                            FROM observations o JOIN f USING (valid_time)
                            WHERE o.series_id = 'eia.DF' AND o.entity_id = ?
                              AND o.observed_at >= ?
                              AND abs(o.value - f.value) > 0.05 * abs(f.value))
                    SELECT d.valid_time, d.value, f.value FROM d JOIN f USING (valid_time)
                    WHERE d.valid_time NOT IN (SELECT valid_time FROM unsettled)
                      AND d.valid_time <= ?
                      AND d.value <> f.value  -- EIA's first CAISO demand value is the forecast
                    ORDER BY d.valid_time DESC LIMIT 1
                    """,
                    [ba, ba, ba, now - SETTLE_WINDOW, now - SETTLE_AGE.get(ba, timedelta(0))],
                ).fetchone()
                typical = None
                if row:
                    typical = c.execute(
                        """
                        WITH d AS (SELECT valid_time, value FROM as_of(TIMESTAMP '2100-01-01')
                                   WHERE series_id = 'eia.D' AND entity_id = ?),
                             f AS (SELECT valid_time, value FROM as_of(TIMESTAMP '2100-01-01')
                                   WHERE series_id = 'eia.DF' AND entity_id = ?)
                        SELECT median(100 * (d.value - f.value) / f.value)
                        FROM d JOIN f USING (valid_time)
                        WHERE d.valid_time >= ? - INTERVAL 28 DAY AND d.valid_time < ?
                          AND f.value > 0 AND d.value <> f.value
                        """,
                        [ba, ba, row[0], row[0]],
                    ).fetchone()
                    typical = typical[0] if typical else None
                open_case = next((x for x in cases if x.entity == ba and x.state != "closed"), None)
                last_case = open_case or next((x for x in cases if x.entity == ba), None)
                regions.append(
                    {
                        "entity": ba,
                        "latest_hour": row[0].isoformat() if row else None,
                        "demand_mw": row[1] if row else None,
                        "forecast_mw": row[2] if row else None,
                        "forecast_error_pct": (100 * (row[1] - row[2]) / row[2])
                        if row and row[2]
                        else None,
                        "typical_error_pct": typical,
                        "case_id": last_case.case_id if last_case else None,
                        "case_state": last_case.state if last_case else None,
                        "top_hypothesis": last_case.top_hypothesis if last_case else None,
                    }
                )
            return {
                "regions": regions,
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        finally:
            c.close()

    @app.get("/api/bench")
    def api_bench() -> dict[str, Any]:
        return {"runs": _bench_runs()}

    @app.get("/api/replay/{case_name}/stream")
    def api_replay_stream(case_name: str) -> StreamingResponse:
        import yaml

        spec_path = config.ROOT / "bench" / "replay" / f"{case_name}.yaml"
        if not spec_path.exists():
            raise HTTPException(404, f"unknown replay case {case_name}")
        spec = yaml.safe_load(spec_path.read_text())

        async def gen() -> AsyncIterator[bytes]:
            def event(kind: str, data: Any) -> bytes:
                return f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n".encode()

            yield event(
                "started",
                {"case": case_name, "entity": spec["entity"], "as_of": str(spec["as_of"])},
            )
            c = cur()
            try:
                case = await asyncio.to_thread(
                    investigate,
                    c,
                    llm_factory(),
                    spec["entity"],
                    spec["metric"],
                    _parse(str(spec["as_of"])),
                )
            except Exception as e:  # noqa: BLE001 - the stream must end cleanly
                log.warning("replay %s failed: %s", case_name, e)
                yield event("error", {"message": str(e)})
                yield event("done", {"case_id": None})
                return
            finally:
                c.close()
            assert case.plan is not None and case.conclusion is not None
            yield event(
                "plan",
                {
                    "hypotheses": case.plan.hypotheses,
                    "steps": [s.model_dump(mode="json") for s in case.plan.steps],
                    "attempts": case.plan_attempts,
                },
            )
            for r in case.receipts:
                yield event(
                    "receipt",
                    {
                        "tool": r.tool,
                        "receipt_id": r.receipt_id,
                        "effect_size": r.effect_size,
                        "inputs": r.inputs,
                    },
                )
                await asyncio.sleep(0.15)
            yield event("conclusion", case.conclusion.model_dump(mode="json"))
            yield event("done", {"case_id": case.case_id})

        return StreamingResponse(gen(), media_type="text/event-stream")

    # A Route, not a Mount: Mount("/mcp") only matches "/mcp/...", so a bare POST /mcp would
    # fall through to the static files mount at "/" and answer 405.
    app.router.routes.append(Route("/mcp", endpoint=mcp_app, methods=["GET", "POST", "DELETE"]))
    if WEB_DIST.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="ui")
    return app


def _bench_runs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    results = config.ROOT / "bench" / "results"
    if not results.exists():
        return out
    for p in sorted(results.glob("*.json")):
        r = SuiteResult.model_validate_json(p.read_text())
        s = score(r)
        out.append(
            {
                "name": p.stem,
                "config": r.config,
                "summary": s,
                "replay": [c.model_dump(mode="json") for c in r.replay],
            }
        )
    return out


def _live_ingest(c: duckdb.DuckDBPyConnection) -> None:
    from gridcase.cli import build_source, live_window
    from gridcase.ingest.runner import run_ingest

    start, end = live_window(datetime.now(UTC).replace(tzinfo=None))
    try:
        for name in ("eia", "openmeteo"):
            run_ingest(c, build_source(name, live=True), start, end, "live")
    finally:
        c.close()
