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

import json
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
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server import MCPServer
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.routing import Route

from gridcase import config
from gridcase.ask.context import case_context, now_context
from gridcase.ask.engine import Asker, answer_question, case_checker, checker_at
from gridcase.ask.llm import default_asker_factory
from gridcase.ask.openrouter import ChatModel, default_chat_factory
from gridcase.catalog import catalog
from gridcase.definitions import definitions
from gridcase.digest import build_digest, due_day, get_digest, latest_digest, list_digests
from gridcase.explore.chat import ExploreAsk, answer_explore
from gridcase.explore.query import ExploreFilter, public_kpis
from gridcase.ingest.export import export_markets
from gridcase.ingest.polymarket_load import GAMMA_URL, refresh
from gridcase.investigate import investigate
from gridcase.market_alarm import WatchedSeries, hourly, load_watch, series_view
from gridcase.market_watch import watch_markets
from gridcase.memory.cases import CaseRecord, get_case, list_cases
from gridcase.memory.retractions import get_retraction, public_cases
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


class DbtDocsFiles(StaticFiles):
    """Docs may be generated after startup, including their target directory."""

    async def check_config(self) -> None:
        # Missing docs are a normal 404; StaticFiles still resolves every request safely.
        if self.directory is not None and Path(self.directory).exists():
            await super().check_config()


def _parse(t: str) -> datetime:
    return (
        datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        if "+" in t or t.endswith("Z")
        else datetime.fromisoformat(t)
    )


# A case whose opening data check found the record this far behind was judged on missing data:
# the collector was down, so the empty hours read as a drop (the 2026-10-21 01:45 alarms).
STALE_AT_OPENING_MIN = 60.0
NOW_PAGE_CASES = 10


def judged_without_data(c: CaseRecord) -> bool:
    """True when the case's own data.coverage, run when it opened, found the record stale."""
    return any(
        r.tool == "data.coverage"
        and r.as_of == c.as_of
        and (r.output.get("freshness_minutes") or 0) > STALE_AT_OPENING_MIN
        for r in c.receipts
    )


def now_cases(cases: list[CaseRecord]) -> list[dict[str, Any]]:
    """The Now page's latest investigations: cases judged on missing data stay on their own page
    but are left out of the list (user decision 2026-10-23)."""
    return [_case_summary(x) for x in cases if not judged_without_data(x)][:NOW_PAGE_CASES]


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
        "judged_without_data": judged_without_data(c),
    }


def _retracted(conn: duckdb.DuckDBPyConnection, case_id: str) -> dict[str, Any] | None:
    """A retracted case's {at, reason, span}; None when it stands."""
    r = get_retraction(conn, case_id)
    if r is None:
        return None
    span = [r.span_from.isoformat(), r.span_to.isoformat()] if r.span_from and r.span_to else None
    return {"at": r.retracted_at.isoformat(), "reason": r.reason, "span": span}


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

    def __init__(
        self, per_window: int, window_s: float, daily_usd: float, what: str = "questions"
    ) -> None:
        self.per_window, self.window_s, self.daily_usd = per_window, window_s, daily_usd
        self.what = what
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
                raise HTTPException(429, f"Too many {self.what}; please wait a few minutes.")
            q.append(now)

    def charge(self, usd: float) -> None:
        with self.lock:
            self.spent[datetime.now(UTC).date().isoformat()] += usd


class ExploreBudget:
    """Paid investigations visitors may start from the Explore page per UTC day; in memory,
    like the question box's budget (reset on restart)."""

    def __init__(self, per_day: int) -> None:
        self.per_day = per_day
        self.used: dict[str, int] = defaultdict(int)
        self.lock = threading.Lock()

    def take(self, day: date) -> bool:
        with self.lock:
            if self.used[day.isoformat()] >= self.per_day:
                return False
            self.used[day.isoformat()] += 1
            return True


class ExploreInvestigate(BaseModel):
    kpi: str
    filters: list[ExploreFilter] = Field(default_factory=list)
    hour: datetime

    @field_validator("kpi")
    @classmethod
    def _public_kpi(cls, v: str) -> str:
        if v not in public_kpis():
            raise ValueError(f"unknown KPI {v!r}")
        return v

    @field_validator("hour")
    @classmethod
    def _whole_hour(cls, v: datetime) -> datetime:
        v = v.astimezone(UTC).replace(tzinfo=None) if v.tzinfo else v
        if (v.minute, v.second, v.microsecond) != (0, 0, 0):
            raise ValueError("hour must be the start of an hour")
        return v


def _explore_filters(segment: str) -> dict[str, list[str]]:
    """A segment label ('category=Sports;size_band=$1k+') checked against the semantic layer."""
    try:
        parsed = parse_segment(segment)
        return {
            f.dimension: f.values
            for f in (
                ExploreFilter(dimension=d, values=[v] if isinstance(v, str) else list(v))
                for d, v in parsed.items()
            )
        }
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, f"unknown segment {segment!r}") from e


def _case_window(case: CaseRecord) -> tuple[datetime, datetime] | None:
    compare = next((r for r in case.receipts if r.tool == "kpi.compare"), None)
    w = compare.inputs.get("window") if compare else None
    return (datetime.fromisoformat(w[0]), datetime.fromisoformat(w[1])) if w else None


def now_payload(c: duckdb.DuckDBPyConnection, at: datetime) -> Any:
    """The Now page's data at `at`: every watched KPI against its usual band, freshness and the
    latest investigations."""
    watched, _ = load_watch()
    series = [series_view(c, w, at) for w in watched]
    row = c.execute(
        "SELECT max(ts), max(collected_at), min(ts) FROM trades_as_of(?)", [at]
    ).fetchone()
    newest, collected, first = row if row else (None, None, None)
    cases = now_cases(public_cases(c))  # retracted cases left out
    for x in cases:
        x["segment"] = x["entity"]
    return jsonable_encoder(
        {
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
    )


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
    chat_factory: Callable[[], ChatModel] | None = None,
    free_llm_factory: Callable[[], LLM] | None = None,
    explore_paid_per_day: int | None = None,
    explore_series_per_window: int = 120,
    snapshot_pages: bool = False,
) -> FastAPI:
    db_path = db_path or config.DB_PATH
    token = token if token is not None else config.api_token()
    llm_factory = llm_factory or AnthropicLLM
    asker_factory = asker_factory or default_asker_factory()
    chat_factory = chat_factory or default_chat_factory()
    free_llm_factory = free_llm_factory or FakeLLM  # the standard playbook: no model, no cost
    # charts are free but not costless on a shared host: a per-visitor ceiling no person reaches
    series_limiter = AskLimiter(explore_series_per_window, 600.0, float("inf"), "chart requests")
    explore_budget = ExploreBudget(
        config.explore_paid_per_day() if explore_paid_per_day is None else explore_paid_per_day
    )
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

    # ------------------------------------------------------------------ page snapshots
    # The Now page and the catalog change only when new trades load, so with snapshot_pages
    # they are computed after each refresh (same code, same single-threaded store) and every
    # request in between is served the stored result. The last snapshot is kept in the store,
    # so a restart serves it at once while the background recomputes.
    pages: dict[str, Any] = {}
    pages_lock = threading.Lock()
    if snapshot_pages:
        for name, body in conn.execute("SELECT name, body FROM page_snapshots").fetchall():
            pages[name] = json.loads(body)

    def refresh_pages() -> None:
        at = now()
        c = cur()
        try:
            fresh = {"now": now_payload(c, at), "catalog": jsonable_encoder(catalog(c, at))}
            for name, body in fresh.items():
                c.execute(
                    "INSERT OR REPLACE INTO page_snapshots VALUES (?, ?, ?)",
                    [name, at, json.dumps(body, separators=(",", ":"))],
                )
        finally:
            c.close()
        with pages_lock:
            pages.update(fresh)

    def page(name: str, build: Callable[[], Any]) -> Any:
        if not snapshot_pages:
            return build()
        with pages_lock:
            if name in pages:
                return pages[name]
        refresh_pages()  # nothing stored yet: compute once, then serve that
        with pages_lock:
            return pages[name]

    mcp = MCPServer(
        "gridcase",
        instructions=(
            "Governed investigation engine for plan-vs-actual misses on the US grid. Read cases, "
            "run the deterministic tools at an as_of, or start an investigation (token required)."
        ),
    )

    @mcp.tool()
    def list_open_cases() -> dict[str, Any]:
        """Cases that are not closed or retracted, newest first."""
        c = cur()
        try:
            return {"cases": [_case_summary(x) for x in public_cases(c) if x.state != "closed"]}
        finally:
            c.close()

    @mcp.tool(name="get_case")
    def get_case_mcp(case_id: str) -> dict[str, Any]:
        """A case with its plan, conclusion, receipts (with SQL) and re-check."""
        c = cur()
        try:
            return {**_case_full(get_case(c, case_id)), "retracted": _retracted(c, case_id)}
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
                            c, wall, llm_factory, free_llm_factory, config.auto_paid_cases_per_day()
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
                if snapshot_pages:
                    try:
                        refresh_pages()
                    except Exception as e:  # noqa: BLE001
                        log.warning("page snapshot failed: %s", e)
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
    app.state.refresh_pages = refresh_pages

    # ------------------------------------------------------------------ JSON API

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "time": datetime.now(UTC).isoformat(timespec="seconds")}

    @app.get("/api/cases")
    def api_cases(include_retracted: bool = False) -> list[dict[str, Any]]:
        """Every case, newest first; retracted cases only with ?include_retracted=1."""
        c = cur()
        try:
            if not include_retracted:
                return [_case_summary(x) for x in public_cases(c)]
            return [
                {**_case_summary(x), "retracted": _retracted(c, x.case_id)} for x in list_cases(c)
            ]
        finally:
            c.close()

    @app.get("/api/cases/{case_id}")
    def api_case(case_id: str) -> dict[str, Any]:
        c = cur()
        try:
            return {**_case_full(get_case(c, case_id)), "retracted": _retracted(c, case_id)}
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
    def api_now() -> Any:
        """The Now page: every watched KPI's last 48 hours against its usual band, how fresh
        our data is, and the latest investigations."""

        def build() -> Any:
            c = cur()
            try:
                return now_payload(c, now())
            finally:
                c.close()

        return page("now", build)

    @app.get("/api/catalog")
    def api_catalog() -> Any:
        """Every KPI in plain words, its governed definition and its last complete hour."""

        def build() -> Any:
            c = cur()
            try:
                return jsonable_encoder(catalog(c, now()))
            finally:
                c.close()

        return page("catalog", build)

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

    @app.post("/api/explore/ask")
    def api_explore_ask(req: ExploreAsk, request: Request) -> dict[str, Any]:
        """The Explore chat (hard rule 6): the model fills in semantic-layer queries, the
        server runs them, and every sentence is verified against this request's evidence.
        Shares the question box's per-visitor limit and daily budget."""
        limiter.admit(visitor(request))
        c = cur()
        try:
            res = answer_explore(chat_factory(), req, c, now())
        finally:
            c.close()
        limiter.charge(res.usage.cost_usd)
        return {
            "sentences": [cl.model_dump() for cl in res.sentences],
            "refused": res.refused,
            "investigate_hour": res.investigate_hour.isoformat() if res.investigate_hour else None,
            "queries": [q.model_dump(mode="json") for q in res.queries],
            "new_receipts": [r.model_dump(mode="json") for r in res.new_receipts],
            "dropped": res.dropped,
            "fallback": res.fallback,
            "page_receipt": res.page_receipt.model_dump(mode="json") if res.page_receipt else None,
            "cost_usd": round(res.usage.cost_usd, 4),
        }

    explore_jobs: dict[str, dict[str, Any]] = {}  # task id -> case id (once open) and paid
    in_flight: dict[tuple[str, str, datetime], str] = {}  # (kpi, segment, hour) -> task id

    record_start: list[str | None] = [None]  # the first trade never changes once collected

    @app.get("/api/definitions")
    def api_definitions() -> dict[str, Any]:
        """How every number on the site is calculated: KPI definitions, slice SQL, the usual
        range rule and the data sources. Static apart from when our record begins."""
        if record_start[0] is None:
            c = cur()
            try:
                row = c.execute("SELECT min(ts) FROM trades").fetchone()
            finally:
                c.close()
            record_start[0] = row[0].isoformat() if row and row[0] else None
        return {**definitions(), "record_start": record_start[0]}

    @app.get("/api/explore/series")
    def api_explore_series(
        request: Request, kpi: str, segment: str = "all", hours: int = 72
    ) -> dict[str, Any]:
        """Any public KPI in any fixed-segment slice, hour by hour against its usual band (the
        watch's own rule). Free: the semantic layer only."""
        series_limiter.admit(visitor(request))
        if kpi not in public_kpis():
            raise HTTPException(422, f"unknown KPI {kpi!r}")
        if not 1 <= hours <= 168:
            raise HTTPException(422, "hours must be between 1 and 168")
        filters = _explore_filters(segment)
        c = cur()
        try:
            return series_view(c, WatchedSeries(kpi=kpi, filters=dict(filters)), now(), hours)
        finally:
            c.close()

    @app.post("/api/explore/investigate", status_code=202)
    def api_explore_investigate(req: ExploreInvestigate, request: Request) -> Any:
        """'Why did this happen?' for one hour of one series. Returns an existing case for that
        hour, refuses hours the watch would not judge, else starts an investigation of exactly
        that hour with the data as it was known when the hour settled. The first
        `explore_paid_per_day` a day use the paid model; the rest, the free standard playbook."""
        p = load_watch()[1]
        filters = {f.dimension: f.values for f in req.filters}
        series = WatchedSeries(kpi=req.kpi, filters=dict(filters))
        hour, settled_at = req.hour, req.hour + timedelta(hours=1, minutes=p.settle_minutes)
        if settled_at > now():
            raise HTTPException(
                409, f"That hour is not finished yet; try again after {settled_at:%H:%M} UTC."
            )
        c = cur()
        try:
            for case in list_cases(c):
                w = _case_window(case) if case.metric == req.kpi else None
                if case.entity == series.segment and w and w[0] <= hour < w[1]:
                    return JSONResponse({"case_id": case.case_id, "existing": True}, 200)
            row = hourly(c, series, settled_at, p, 1)
        finally:
            c.close()
        if row.gap_hours_skipped:
            raise HTTPException(
                409, "Our collector missed trades in that hour, so it is never judged."
            )
        if row.rows[0]["expected"] is None:
            raise HTTPException(
                409, "It's too early to judge that hour: there isn't enough history for it yet."
            )
        key = (req.kpi, series.segment, hour)
        running = in_flight.get(key)
        if running and registry.get(running)["status"] in ("queued", "running"):
            return {"task_id": running, "paid": explore_jobs[running]["paid"]}
        limiter.admit(visitor(request))
        paid = explore_budget.take(now().date())
        factory = llm_factory if paid else free_llm_factory
        holder: dict[str, Any] = {"case_id": None, "paid": paid}

        def job() -> dict[str, Any]:
            c2 = cur()
            try:
                case = investigate(
                    c2,
                    factory(),
                    req.kpi,
                    settled_at,
                    filters=dict(filters),
                    window=(hour, hour + timedelta(hours=1)),
                    on_open=lambda case_id: holder.__setitem__("case_id", case_id),
                )
            finally:
                c2.close()
            return {"case_id": case.case_id}

        task_id = registry.submit(job, kind="explore")
        if len(explore_jobs) > 1000:  # the registry forgets old tasks too
            explore_jobs.pop(next(iter(explore_jobs)))
        explore_jobs[task_id] = holder
        in_flight[key] = task_id
        return {"task_id": task_id, "paid": paid}

    @app.get("/api/explore/investigations/{task_id}")
    def api_explore_investigation(task_id: str) -> dict[str, Any]:
        """Poll an Explore investigation: queued, running, done or failed, with its case id as
        soon as the case exists (the case page then shows its progress)."""
        holder = explore_jobs.get(task_id)
        try:
            t = registry.get(task_id)
        except KeyError:
            t = None
        if holder is None or t is None:
            raise HTTPException(404, f"unknown investigation {task_id}")
        failed = t["status"] == "failed"
        return {
            "status": t["status"],
            "case_id": holder["case_id"],
            "paid": holder["paid"],
            "error": "The investigation failed; please try again." if failed else None,
        }

    app.router.routes.append(Route("/mcp", endpoint=mcp_app, methods=["GET", "POST", "DELETE"]))
    docs = dbt_docs_dir or config.DBT_DIR / "target"
    app.mount(
        "/dbt", DbtDocsFiles(directory=str(docs), check_dir=False, html=True), name="dbt-docs"
    )
    if WEB_DIST.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="ui")
    app.add_middleware(PublicEdge)
    return app
