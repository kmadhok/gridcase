"""M15 history tools: point-in-time reads of backfilled prices and market metadata.

They run on data/backfill.duckdb (built by `gridcase backfill build`) and read only through
prices_as_of(as_of) and hist_markets_as_of(as_of), so a task replayed at as_of never sees a
later price or a market that had not started. Outputs never carry an outcome, a market's
lifetime volume (it includes trading after as_of) or a close time later than as_of. SQL is
fixed; inputs are bound parameters.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

from gridcase.tools.base import Rows, ToolCtx, ToolError, tool
from gridcase.tools.registry import HistMarket, HistMove, HistRelated, HistSeries, HistSiblings

GRAINS = {"10m": (timedelta(minutes=10), "10 minutes"), "1h": (timedelta(hours=1), "1 hour"),
          "1d": (timedelta(days=1), "1 day")}  # fmt: skip
MAX_POINTS = 300
LOOKBACK = timedelta(days=30)  # a price older than this is not "the price now"
MOVE_HOURS = (1, 6, 24, 72)


def _price_at(ctx: ToolCtx, cid: str, t: datetime) -> tuple[datetime, float] | None:
    rows = ctx.raw(
        "SELECT ts, p FROM prices_as_of(?) WHERE condition_id = ? AND ts <= ? AND ts > ? "
        "ORDER BY ts DESC LIMIT 1",
        [ctx.as_of, cid, t, t - LOOKBACK],
    )
    return (rows[0][0], float(rows[0][1])) if rows else None


def _market(ctx: ToolCtx, cid: str) -> Rows:
    return ctx.raw(
        "SELECT question, event_slug, event_title, category, tags, outcome0, outcome1, "
        "start_at, end_at, CASE WHEN closed_at <= ? THEN closed_at END "
        "FROM hist_markets_as_of(?) WHERE condition_id = ?",
        [ctx.as_of, ctx.as_of, cid],
    )


def _moves(
    ctx: ToolCtx, ids_sql: str, params: list[Any], hours: int
) -> dict[str, tuple[float | None, float | None]]:
    """condition_id -> (price at as_of, price `hours` before) for the markets `ids_sql` selects."""
    then = ctx.as_of - timedelta(hours=hours)
    rows = ctx.raw(
        f"""
        WITH ids AS ({ids_sql}),
        now_p AS (SELECT condition_id, arg_max(p, ts) AS p FROM prices_as_of(?)
                  WHERE condition_id IN (SELECT condition_id FROM ids) AND ts > ?
                  GROUP BY condition_id),
        then_p AS (SELECT condition_id, arg_max(p, ts) AS p FROM prices_as_of(?)
                   WHERE condition_id IN (SELECT condition_id FROM ids) AND ts <= ? AND ts > ?
                   GROUP BY condition_id)
        SELECT ids.condition_id, now_p.p, then_p.p
        FROM ids LEFT JOIN now_p USING (condition_id) LEFT JOIN then_p USING (condition_id)
        ORDER BY ids.condition_id
        """,
        [*params, ctx.as_of, ctx.as_of - LOOKBACK, ctx.as_of, then, then - LOOKBACK],
    )
    return {r[0]: (r[1], r[2]) for r in rows}


def _change(now: float | None, then: float | None) -> float | None:
    return None if now is None or then is None else now - then


@tool("hist.market")
def hist_market(ctx: ToolCtx, inp: HistMarket) -> tuple[dict[str, Any], float | None]:
    rows = _market(ctx, inp.condition_id)
    if not rows:
        return {"found": False}, None
    q, ev, ev_title, cat, tags, o0, o1, start, end, closed = rows[0]
    n = ctx.raw("SELECT count(*) FROM hist_markets_as_of(?) WHERE event_slug = ?", [ctx.as_of, ev])
    return {
        "found": True, "condition_id": inp.condition_id, "question": q, "event": ev,
        "event_title": ev_title, "category": cat, "tags": [t for t in (tags or "").split("|") if t],
        "outcomes": [o0, o1], "price_is_for": o0, "start_at": start, "scheduled_end": end,
        "closed_at": closed, "markets_in_event": int(n[0][0]) if n else 0,
    }, None  # fmt: skip


@tool("hist.series")
def hist_series(ctx: ToolCtx, inp: HistSeries) -> tuple[dict[str, Any], float | None]:
    step, interval = GRAINS[inp.grain]
    n = timedelta(hours=inp.hours) / step
    if n > MAX_POINTS:
        raise ToolError(
            f"hist.series: {int(n)} points is over {MAX_POINTS}; use a coarser grain or fewer hours"
        )
    rows = ctx.raw(
        f"SELECT time_bucket(INTERVAL '{interval}', ts) AS t, arg_max(p, ts) "
        "FROM prices_as_of(?) WHERE condition_id = ? AND ts > ? GROUP BY 1 ORDER BY 1",
        [ctx.as_of, inp.condition_id, ctx.as_of - timedelta(hours=inp.hours)],
    )
    points = [[t, float(p)] for t, p in rows]
    return {
        "condition_id": inp.condition_id,
        "grain": inp.grain,
        "hours": inp.hours,
        "points": points,
        "first": points[0] if points else None,
        "last": points[-1] if points else None,
    }, None


@tool("hist.move")
def hist_move(ctx: ToolCtx, inp: HistMove) -> tuple[dict[str, Any], float | None]:
    now = _price_at(ctx, inp.condition_id, ctx.as_of)
    if now is None:
        return {"condition_id": inp.condition_id, "price": None}, None
    change: dict[str, float | None] = {}
    for h in MOVE_HOURS:
        then = _price_at(ctx, inp.condition_id, ctx.as_of - timedelta(hours=h))
        change[f"{h}h"] = None if then is None else now[1] - then[1]
    hourly = ctx.raw(
        "SELECT time_bucket(INTERVAL '1 hour', ts) AS t, arg_max(p, ts) FROM prices_as_of(?) "
        "WHERE condition_id = ? AND ts > ? GROUP BY 1 ORDER BY 1",
        [ctx.as_of, inp.condition_id, ctx.as_of - timedelta(days=7)],
    )
    diffs = [float(b[1]) - float(a[1]) for a, b in zip(hourly, hourly[1:], strict=False)]
    return {
        "condition_id": inp.condition_id,
        "price": now[1],
        "price_at": now[0],
        "minutes_since_last_point": int((ctx.as_of - now[0]).total_seconds() // 60),
        "change": change,
        "hourly_change_sd_7d": statistics.pstdev(diffs) if len(diffs) > 1 else None,
    }, change["6h"]


@tool("hist.siblings")
def hist_siblings(ctx: ToolCtx, inp: HistSiblings) -> tuple[dict[str, Any], float | None]:
    own = _market(ctx, inp.condition_id)
    if not own:
        return {"found": False}, None
    ev = own[0][1]
    ids = (
        "SELECT condition_id FROM hist_markets_as_of(?) WHERE event_slug = ? AND condition_id <> ?"
    )
    moves = _moves(ctx, ids, [ctx.as_of, ev, inp.condition_id], inp.hours)
    names = dict(
        (r[0], r[1])
        for r in ctx.raw(
            "SELECT condition_id, question FROM hist_markets_as_of(?) WHERE event_slug = ?",
            [ctx.as_of, ev],
        )
    )
    ranked = sorted(moves.items(), key=lambda kv: (-abs(_change(*kv[1]) or 0.0), kv[0]))
    sibs = [
        {"condition_id": c, "question": names.get(c), "price": now, "change": _change(now, then)}
        for c, (now, then) in ranked
    ]
    return {
        "event": ev,
        "window_hours": inp.hours,
        "n_siblings": len(sibs),
        "siblings": sibs[: inp.top_n],
    }, None


def _generic_tags(ctx: ToolCtx) -> set[str]:
    """Tags too broad to say two events share news: on more than 2% of the events known at
    as_of (and on at least 3)."""
    rows = ctx.raw(
        """
        WITH e AS (SELECT event_slug, any_value(tags) AS tags FROM hist_markets_as_of(?)
                   GROUP BY event_slug),
        t AS (SELECT DISTINCT event_slug, unnest(string_split(tags, '|')) AS tag FROM e)
        SELECT tag FROM t WHERE tag <> '' GROUP BY tag
        HAVING count(*) > greatest(2, 0.02 * (SELECT count(*) FROM e)) ORDER BY tag
        """,
        [ctx.as_of],
    )
    return {r[0] for r in rows}


@tool("hist.related")
def hist_related(ctx: ToolCtx, inp: HistRelated) -> tuple[dict[str, Any], float | None]:
    own = _market(ctx, inp.condition_id)
    if not own:
        return {"found": False}, None
    ev, tags = own[0][1], own[0][4]
    specific = sorted({t for t in (tags or "").split("|") if t} - _generic_tags(ctx))
    if not specific:
        return {"specific_tags": [], "window_hours": inp.hours, "related": []}, None
    ids = (
        "SELECT condition_id FROM hist_markets_as_of(?) WHERE event_slug <> ? "
        "AND list_has_any(string_split(tags, '|'), ?::VARCHAR[])"
    )
    moves = _moves(ctx, ids, [ctx.as_of, ev, specific], inp.hours)
    info = {
        r[0]: (r[1], r[2])
        for r in ctx.raw(
            "SELECT condition_id, question, event_slug FROM hist_markets_as_of(?) "
            "WHERE event_slug <> ? AND list_has_any(string_split(tags, '|'), ?::VARCHAR[])",
            [ctx.as_of, ev, specific],
        )
    }
    out = []
    for c, (now, then) in moves.items():
        ch = _change(now, then)
        if ch is not None and abs(ch) * 100 >= inp.min_move_pts:
            q, e = info.get(c, (None, None))
            out.append({"condition_id": c, "question": q, "event": e, "price": now, "change": ch})
    out.sort(key=lambda r: (-abs(r["change"]), r["condition_id"]))
    return {
        "specific_tags": specific,
        "window_hours": inp.hours,
        "n_related": len(out),
        "related": out[: inp.top_n],
    }, None
