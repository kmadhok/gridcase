"""The MarketCase service.

* MCP (2026-07-28 stateless Streamable HTTP at /mcp): `list_open_cases`, `get_case`,
  `investigate` (returns a task handle; needs the API token), `get_task`, and every tool
  read-only as `tool_<name>`. Nothing that mutates the ontology.
* JSON API for the UI at /api/*, read-only apart from the rate-limited question box.
* The built UI (web/dist) as static files at /.

One DuckDB connection is shared through cursors: DuckDB allows one process per file, so the
service itself loads collected trades every few minutes, runs the hourly watch and the
follow-up checks in a background thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from mcp.server import MCPServer
from pydantic import BaseModel, Field
from starlette.routing import Route

from gridcase import config
from gridcase.ask.context import case_context, now_context
from gridcase.ask.engine import Asker, answer_question, case_checker, checker_at
from gridcase.ask.llm import default_asker_factory
from gridcase.catalog import catalog
from gridcase.digest import build_digest, due_day, get_digest, latest_digest, list_digests
from gridcase.ingest.export import export_markets
from gridcase.ingest.polymarket_load import GAMMA_URL, refresh
from gridcase.investigate import investigate
from gridcase.market_alarm import load_watch, series_view
from gridcase.market_watch import watch_markets
from gridcase.memory.cases import CaseRecord, get_case, list_cases
from gridcase.planner.llm import LLM, AnthropicLLM, FakeLLM
from gridcase.semantic.kpis import load_kpis, parse_segment
from gridcase.server.scheduler import drain_scheduled
from gridcase.server.tasks import TaskRegistry
from gridcase.store.db import connect
from gridcase.tools.base import run_tool
from gridcase.tools.registry import TOOL_INPUTS, TOOL_NAMES
from gridcase.trace import read_trace

log = logging.getLogger("gridcase")
WEB_DIST = config.ROOT / "web" / "dist"
DIGEST_SETTLE = timedelta(minutes=30)  # the last trades of the day are collected by then


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


PUBLIC_PREFIX = "/marketcase"  # kanumadhok.com/marketcase forwards here with its path
TUNNEL_HEADER = b"cf-connecting-ip"  # added by Cloudflare to everything through the tunnel


class PublicEdge:
    """ASGI wrapper for the public route: serve everything under /marketcase as well as at /,
    and refuse MCP (which can start paid investigations) to anything that came through the
    Cloudflare Tunnel. Tailscale requests carry no Cloudflare header and are unaffected."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            path: str = scope["path"]
            if path == PUBLIC_PREFIX or path.startswith(PUBLIC_PREFIX + "/"):
                path = path[len(PUBLIC_PREFIX) :] or "/"
                scope = {**scope, "path": path, "raw_path": path.encode()}
            public = any(k == TUNNEL_HEADER for k, _ in scope["headers"])
            if public and (path == "/mcp" or path.startswith("/mcp/")):
                await send({"type": "http.response.start", "status": 404, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


def visitor(request: Request) -> str:
    """Who is asking, for the per-visitor limit: through the tunnel, the first address in
    X-Forwarded-For (the visitor, ahead of Vercel's and Cloudflare's hops); else the peer."""
    if TUNNEL_HEADER.decode() in request.headers:
        first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        return first or request.headers[TUNNEL_HEADER.decode()]
    return request.client.host if request.client else "?"


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    case_id: str | None = None
    page: Literal["now"] | None = None


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
    watch: bool = False,
    polymarket_interval_s: float | None = None,
    clock: Callable[[], datetime] | None = None,
    asker_factory: Callable[[], Asker] | None = None,
    ask_per_window: int = 10,
    ask_window_s: float = 600.0,
    ask_daily_usd: float | None = None,
    dbt_docs_dir: Path | None = None,
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

    def now() -> datetime:
        return clock() if clock else datetime.now(UTC).replace(tzinfo=None)

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
    def investigate_mcp(kpi: str, as_of: str, token: str, segment: str = "all") -> dict[str, Any]:
        """Why did a KPI move? Starts an investigation of `kpi` in `segment` ('all' or e.g.
        'category=Sports') over the hours before as_of and returns a task handle to poll with
        get_task. Requires the API token."""
        if not token or token != app.state.token:
            raise ValueError("unauthorized: bad or missing token")
        if kpi not in load_kpis()[0]:
            raise ValueError(f"unknown KPI {kpi}")
        when = _parse(as_of)
        filters = parse_segment(segment)

        def job() -> dict[str, Any]:
            c = cur()
            try:
                case = investigate(c, llm_factory(), kpi, when, filters=filters)
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

    # ------------------------------------------------------------------ background work

    def background() -> None:
        next_drain = time.monotonic()
        next_polymarket = time.monotonic()
        last_market_watch = [datetime.min]
        gamma = httpx.Client(base_url=GAMMA_URL, headers={"User-Agent": "marketcase"})
        while not stop.is_set():
            now = time.monotonic()
            if polymarket_interval_s and now >= next_polymarket:
                c = cur()
                try:
                    r = refresh(c, config.RAW_DIR / "polymarket", gamma)
                    log.info(
                        "polymarket refresh: %d new trades, %d new markets", r.new, r.markets_added
                    )
                    export_markets(c, config.EXPORT_DIR / "markets.parquet")
                except Exception as e:  # noqa: BLE001
                    log.warning("polymarket refresh failed: %s", e)
                finally:
                    c.close()
                next_polymarket = now + polymarket_interval_s
                wall = datetime.now(UTC).replace(tzinfo=None)
                settled = (wall - timedelta(minutes=15)).replace(minute=0, second=0, microsecond=0)
                if watch and settled > last_market_watch[0]:  # a new hour has settled
                    last_market_watch[0] = settled
                    c = cur()
                    try:
                        watch_markets(
                            c, wall, llm_factory, FakeLLM, config.auto_paid_cases_per_day()
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("market watch failed: %s", e)
                    finally:
                        c.close()
                if watch:
                    c = cur()
                    try:
                        day = due_day(c, wall, DIGEST_SETTLE)
                        if day is not None:
                            build_digest(c, day, wall)
                            log.info("digest built for %s", day)
                    except Exception as e:  # noqa: BLE001
                        log.warning("digest failed: %s", e)
                    finally:
                        c.close()
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
        if drain_interval_s or polymarket_interval_s:
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

    @app.get("/api/cases/{case_id}/trace")
    def api_case_trace(case_id: str) -> dict[str, Any]:
        c = cur()
        try:
            get_case(c, case_id)
            return read_trace(c, case_id)
        except KeyError as e:
            raise HTTPException(404, f"unknown case {case_id}") from e
        finally:
            c.close()

    @app.get("/api/now")
    def api_now() -> dict[str, Any]:
        """The Now page: every watched KPI's last 48 hours against its usual band, how fresh
        our data is, and the latest investigations."""
        at = now()
        c = cur()
        try:
            watched, _ = load_watch()
            series = [series_view(c, w, at) for w in watched]
            row = c.execute(
                "SELECT max(ts), max(collected_at), min(ts) FROM trades_as_of(?)", [at]
            ).fetchone()
            newest, collected, first = row if row else (None, None, None)
            cases = [_case_summary(x) for x in list_cases(c)[:10]]
        finally:
            c.close()
        for x in cases:
            x["segment"] = x["entity"]
        return {
            "as_of": at.isoformat(),
            "series": series,
            "freshness": {
                "newest_trade": newest.isoformat() if newest else None,
                "record_starts": first.isoformat() if first else None,
                "minutes_behind": round((at - collected).total_seconds() / 60, 1)
                if collected
                else None,
            },
            "cases": cases,
        }

    @app.get("/api/catalog")
    def api_catalog() -> dict[str, Any]:
        """Every KPI in plain words, its governed definition and its last complete hour."""
        c = cur()
        try:
            return catalog(c, now())
        finally:
            c.close()

    @app.get("/api/digests")
    def api_digests() -> list[dict[str, str]]:
        c = cur()
        try:
            return list_digests(c)
        finally:
            c.close()

    @app.get("/api/digest")
    def api_digest_latest() -> dict[str, Any]:
        """The newest morning read."""
        c = cur()
        try:
            d = latest_digest(c)
        finally:
            c.close()
        if d is None:
            raise HTTPException(404, "no digest yet")
        return d

    @app.get("/api/digest/{day}")
    def api_digest(day: str) -> dict[str, Any]:
        try:
            when = date.fromisoformat(day)
        except ValueError as e:
            raise HTTPException(422, "day must be YYYY-MM-DD") from e
        c = cur()
        try:
            d = get_digest(c, when)
        finally:
            c.close()
        if d is None:
            raise HTTPException(404, f"no digest for {day}")
        return d

    @app.post("/api/ask")
    def api_ask(req: AskRequest, request: Request) -> dict[str, Any]:
        """Evidence-only answers (hard rule 6): about a case (read-only checks at its as_of)
        or the Now page (checks at the current time). Every sentence is verified like the
        conclusion."""
        if not req.case_id and req.page != "now":
            raise HTTPException(422, "ask about an investigation (case_id) or the Now page")
        c = cur()
        try:
            if req.case_id:
                try:
                    case = get_case(c, req.case_id)
                except KeyError as e:
                    raise HTTPException(404, f"unknown case {req.case_id}") from e
                limiter.admit(visitor(request))
                res = answer_question(
                    asker_factory(),
                    req.question,
                    context=case_context(case),
                    receipts={r.receipt_id: r for r in case.receipts},
                    run_check=case_checker(c, case),
                )
            else:
                limiter.admit(visitor(request))
                at = now()
                ctx, receipts = now_context(c, at)
                res = answer_question(
                    asker_factory(),
                    req.question,
                    context=ctx,
                    receipts=receipts,
                    run_check=checker_at(c, at),
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

    app.router.routes.append(Route("/mcp", endpoint=mcp_app, methods=["GET", "POST", "DELETE"]))
    docs = dbt_docs_dir or config.DBT_DIR / "target"
    if (docs / "index.html").exists():  # `gridcase dbt docs generate` writes it
        app.mount("/dbt", StaticFiles(directory=str(docs), html=True), name="dbt-docs")
    if WEB_DIST.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="ui")
    app.add_middleware(PublicEdge)
    return app
