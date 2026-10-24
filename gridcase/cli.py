"""`gridcase` command line for MarketCase: collect, refresh, investigate, serve."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from gridcase import config
from gridcase.ingest.polymarket import COLLECT_EVERY_S
from gridcase.planner.llm import LLM
from gridcase.store.db import connect, one


def _parse_date(s: str) -> datetime:
    return (
        datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        if "T" in s
        else datetime.fromisoformat(s)
    )


def cmd_init(_: argparse.Namespace) -> int:
    conn = connect(config.DB_PATH)
    trades, markets = one(
        conn, "SELECT (SELECT count(*) FROM trades), (SELECT count(*) FROM markets)"
    )
    print(f"{config.DB_PATH}: {trades} trades, {markets} markets")
    conn.close()
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from gridcase.ingest.polymarket import run_collector

    run_collector(config.RAW_DIR / "polymarket", interval_s=args.interval)
    return 0


def cmd_fill_gaps(_: argparse.Namespace) -> int:
    """Fill recorded collector gaps from the trades feed's history (about a month back); the
    server's next refresh loads the trades and drops the filled gaps."""
    import httpx

    from gridcase.ingest.polymarket import BASE_URL, fill_gaps

    client = httpx.Client(base_url=BASE_URL, headers={"User-Agent": "gridcase"})
    r = fill_gaps(client, config.RAW_DIR / "polymarket")
    print(
        f"{r.filled} gaps filled ({r.saved} trades over {r.pages} pages); {r.unfilled} left"
        + (f"; stopped on error: {r.error}" if r.error else "")
    )
    return 1 if r.error else 0


def cmd_refresh(_: argparse.Namespace) -> int:
    """Load collected trades and categorise new markets once (the server does this every
    5 minutes; this is for a laptop copy)."""
    import httpx

    from gridcase.ingest.polymarket_load import GAMMA_URL, refresh

    conn = connect(config.DB_PATH)
    r = refresh(conn, config.RAW_DIR / "polymarket", httpx.Client(base_url=GAMMA_URL))
    print(f"{r.files} files, {r.new} new trades, {r.markets_added} new markets, {r.gaps} gaps")
    conn.close()
    return 0


def cmd_investigate(args: argparse.Namespace) -> int:
    from gridcase.investigate import investigate
    from gridcase.semantic.kpis import parse_segment

    llm = choose_llm_factory(args.llm, config.has_anthropic_api_key())()
    as_of = _parse_date(args.as_of) if args.as_of else datetime.now(UTC).replace(tzinfo=None)
    conn = connect(config.DB_PATH)
    case = investigate(conn, llm, args.kpi, as_of, filters=parse_segment(args.segment))
    assert case.conclusion is not None
    print(f"case {case.case_id} top={case.top_hypothesis} cost=${case.llm_usage.cost_usd:.4f}")
    for v in case.conclusion.ranked:
        print(f"  {v.id:18s} {v.status:9s} likely={v.confidence:.2f} covers={v.coverage}")
    for c in case.conclusion.claims:
        print(f"  [{c.tier}] {c.text}")
    for cv in case.conclusion.caveats:
        print(f"  ! {cv}")
    conn.close()
    return 0


@dataclass(frozen=True)
class ServeOptions:
    host: str
    port: int
    drain_interval_s: float | None
    llm: str = "auto"
    watch: bool = True


def cmd_digest(args: argparse.Namespace) -> int:
    """Build (or rebuild) one day's morning read; the server does this after 00:30 UTC."""
    from gridcase.digest import build_digest

    as_of = datetime.now(UTC).replace(tzinfo=None)
    day = date.fromisoformat(args.day) if args.day else as_of.date()
    conn = connect(config.DB_PATH)
    d = build_digest(conn, day, max(as_of, datetime.combine(day, datetime.min.time())))
    conn.close()
    print(f"digest {d['day']} (period {d['period_days']} days)")
    if d["headline"]:
        print(f"  {d['headline']['text']}")
    for c in d["cards"]:
        print(f"  - {c['sentence']['text']}")
    return 0


def cmd_cases(args: argparse.Namespace) -> int:
    """Retract cases judged on hours our record did not have, or undo that. The server is the
    store's only writer, so stop gridcase-server first; the Now page drops retracted cases at
    its next refresh."""
    from gridcase.memory.retractions import outage_cases, retract_case, unretract_case

    conn = connect(config.DB_PATH)
    try:
        if args.cases_cmd == "retract":
            span = _span(args.span_from, args.span_to)
            for cid in args.case_ids:
                try:
                    retract_case(conn, cid, args.reason, span)
                except KeyError:
                    print(f"unknown case {cid}", file=sys.stderr)
                    return 1
                print(f"retracted {cid}: {args.reason}")
            return 0
        if args.cases_cmd == "unretract":
            for cid in args.case_ids:
                print(
                    f"unretracted {cid}"
                    if unretract_case(conn, cid)
                    else f"{cid} was not retracted"
                )
            return 0
        matches = outage_cases(conn, _parse_date(args.since), _parse_date(args.until))
        for m in matches:
            print(
                f"{'(already retracted) ' if m.retracted else ''}{m.case_id}  {m.metric} "
                f"{m.entity}  hours {m.window[0]:%Y-%m-%d %H:%M}-{m.window[1]:%H:%M}  "
                f"opened {m.opened_at:%Y-%m-%d %H:%M}  {m.why} "
                f"{m.span[0]:%Y-%m-%d %H:%M}-{m.span[1]:%Y-%m-%d %H:%M}"
            )
        todo = [m for m in matches if not m.retracted]
        if args.dry_run:
            print(f"{len(matches)} cases match; dry run, nothing written")
            return 0
        for m in todo:
            retract_case(conn, m.case_id, args.reason, m.span)
        print(f"{len(todo)} cases retracted ({len(matches) - len(todo)} already were)")
        return 0
    finally:
        conn.close()


def _span(lo: str | None, hi: str | None) -> tuple[datetime, datetime] | None:
    if (lo is None) != (hi is None):
        raise SystemExit("give both --span-from and --span-to, or neither")
    return (_parse_date(lo), _parse_date(hi)) if lo and hi else None


OUTAGE_REASON = "Data outage: the hours it judged had no data then and were filled later."


def run_dbt(args: list[str]) -> int:
    from dbt.cli.main import dbtRunner

    res = dbtRunner().invoke(args)
    return 0 if res.success else 1


def cmd_dbt(args: argparse.Namespace) -> int:
    """Run dbt on the warehouse project (dbt/) with this deployment's paths: the collector's
    raw files, the server's market export and dbt's own DuckDB file."""
    os.environ.setdefault("MARKETCASE_RAW", str(config.RAW_DIR / "polymarket"))
    os.environ.setdefault("MARKETCASE_EXPORTS", str(config.EXPORT_DIR))
    os.environ.setdefault("MARKETCASE_WAREHOUSE", str(config.WAREHOUSE_PATH))
    dirs = ["--project-dir", str(config.DBT_DIR), "--profiles-dir", str(config.DBT_DIR)]
    return run_dbt([*args.dbt_args, *dirs])


def cmd_warehouse(args: argparse.Namespace) -> int:
    from gridcase.warehouse import check_sources, publish

    if args.warehouse_cmd == "check-sources":
        return check_sources(args.max_age_minutes)
    return publish()


def choose_llm_factory(llm: str, has_key: bool) -> Callable[[], LLM]:
    """auto = the real planner when a key is present, else the template FakeLLM (logged)."""
    from gridcase.planner.llm import AnthropicLLM, FakeLLM

    if llm == "fake" or (llm == "auto" and not has_key):
        return FakeLLM
    return AnthropicLLM


def serve_options(argv: list[str]) -> ServeOptions:
    """`gridcase serve` flags. The server is the single DuckDB writer: it loads collected
    trades, runs the hourly watch and the follow-up checks itself."""
    p = argparse.ArgumentParser(prog="gridcase serve")
    _add_serve_args(p)
    return _serve_options_from(p.parse_args(argv))


def _serve_options_from(a: argparse.Namespace) -> ServeOptions:
    return ServeOptions(
        host=a.host,
        port=a.port,
        drain_interval_s=None if a.no_drain else float(a.drain_interval),
        llm=a.llm,
        watch=not a.no_watch,
    )


def _add_serve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default="127.0.0.1", help="bind address (VPS: the Tailscale IP)")
    p.add_argument("--port", type=int, default=8710)
    p.add_argument("--drain-interval", type=float, default=600.0, help="seconds")
    p.add_argument("--no-drain", action="store_true", help="do not run scheduled re-checks")
    p.add_argument(
        "--no-watch",
        action="store_true",
        help="do not open investigations automatically when a KPI alarm fires",
    )
    p.add_argument(
        "--llm",
        choices=["auto", "fake", "anthropic"],
        default="auto",
        help="planner/explainer model; auto = anthropic if ANTHROPIC_API_KEY is set, else fake",
    )


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from gridcase.server.app import create_app

    opts = _serve_options_from(args)
    factory = choose_llm_factory(opts.llm, config.has_anthropic_api_key())
    if factory.__name__ == "FakeLLM":
        logging.warning("serving with the template FakeLLM (no ANTHROPIC_API_KEY or --llm fake)")
    app = create_app(
        db_path=config.DB_PATH,
        token=config.api_token(),
        llm_factory=factory,
        drain_interval_s=opts.drain_interval_s,
        watch=opts.watch,
        polymarket_interval_s=config.POLYMARKET_LOAD_EVERY_S,
        snapshot_pages=True,  # the Now page and catalog are computed once per refresh
    )
    uvicorn.run(app, host=opts.host, port=opts.port, log_level="info")
    return 0


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL at INFO; keep URLs (and any keys in them) out of the logs.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="gridcase", description="MarketCase")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the store and show what it holds").set_defaults(fn=cmd_init)
    col = sub.add_parser("collect", help="save live Polymarket trades as they happen")
    col.add_argument(
        "--interval", type=float, default=COLLECT_EVERY_S, help="seconds between polls"
    )
    col.set_defaults(fn=cmd_collect)
    fil = sub.add_parser("fill-gaps", help="fill recorded collector gaps from the feed's history")
    fil.set_defaults(fn=cmd_fill_gaps)
    ref = sub.add_parser("refresh", help="load collected trades and categorise markets once")
    ref.set_defaults(fn=cmd_refresh)
    inv = sub.add_parser("investigate", help="why did a KPI move?")
    inv.add_argument("--kpi", required=True, help="e.g. volume_usd, active_traders")
    inv.add_argument("--segment", default="all", help="'all' or e.g. 'category=Sports'")
    inv.add_argument("--as-of", help="UTC; default now")
    inv.add_argument("--llm", choices=["auto", "fake", "anthropic"], default="auto")
    inv.set_defaults(fn=cmd_investigate)
    dig = sub.add_parser("digest", help="build the morning read for a day (default today)")
    dig.add_argument("--day", help="YYYY-MM-DD; covers up to that day's 00:00 UTC")
    dig.set_defaults(fn=cmd_digest)
    cases = sub.add_parser("cases", help="retract cases judged on missing data, or undo that")
    cases.set_defaults(fn=cmd_cases)
    cases_sub = cases.add_subparsers(dest="cases_cmd", required=True)
    ret = cases_sub.add_parser("retract", help="hide cases from the public lists, with a reason")
    ret.add_argument("case_ids", nargs="+", metavar="CASE_ID")
    ret.add_argument("--reason", required=True, help="shown on the case page")
    ret.add_argument("--span-from", help="UTC start of the outage that justifies it (optional)")
    ret.add_argument("--span-to", help="UTC end of that outage")
    unr = cases_sub.add_parser("unretract", help="list retracted cases publicly again")
    unr.add_argument("case_ids", nargs="+", metavar="CASE_ID")
    out = cases_sub.add_parser(
        "retract-outage",
        help="retract cases whose hours overlap a collector gap or were empty then, filled since",
    )
    out.add_argument("--since", required=True, help="UTC; cases whose hours end after this")
    out.add_argument("--until", required=True, help="UTC; cases whose hours start before this")
    out.add_argument("--reason", default=OUTAGE_REASON)
    out.add_argument("--dry-run", action="store_true", help="list the matches, write nothing")
    dbt = sub.add_parser("dbt", help="run dbt on the warehouse (e.g. `gridcase dbt build`)")
    dbt.add_argument("dbt_args", nargs=argparse.REMAINDER, help="passed to dbt")
    dbt.set_defaults(fn=cmd_dbt)
    warehouse = sub.add_parser("warehouse", help="check sources or publish the dbt marts")
    warehouse.set_defaults(fn=cmd_warehouse)
    warehouse_sub = warehouse.add_subparsers(dest="warehouse_cmd", required=True)
    check = warehouse_sub.add_parser("check-sources", help="fail when warehouse inputs are stale")
    check.add_argument("--max-age-minutes", type=float, default=15)
    warehouse_sub.add_parser("publish", help="copy the marts to MotherDuck (or a local file)")
    srv = sub.add_parser("serve", help="run the MCP + JSON API + UI server (single DB writer)")
    _add_serve_args(srv)
    srv.set_defaults(fn=cmd_serve)
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
