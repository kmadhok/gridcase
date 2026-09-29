"""`gridcase` command line. Phase 1: init and ingest. Later phases add replay/bench/serve."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

import httpx

from gridcase import config
from gridcase.ingest.base import Source
from gridcase.ingest.runner import Mode, run_ingest
from gridcase.store.db import connect, one

SOURCES = ["eia", "openmeteo", "gridstatus"]


def _parse_date(s: str) -> datetime:
    return (
        datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        if "T" in s
        else datetime.fromisoformat(s)
    )


def live_window(now: datetime) -> tuple[datetime, datetime]:
    """Hourly mode: re-observe the last 3 days (revisions) and pull forecasts 2 days ahead."""
    top = now.replace(minute=0, second=0, microsecond=0)
    return top - timedelta(days=3), top + timedelta(days=2)


def resolve_window(start: str | None, end: str | None, now: datetime) -> tuple[datetime, datetime]:
    """Backfill window from CLI strings; defaults to the spec's history start and now."""
    s = _parse_date(start) if start else config.HISTORY_START
    e = _parse_date(end) if end else now
    if e <= s:
        raise SystemExit(f"--end {e} must be after --start {s}")
    return s, e


def build_source(name: str, live: bool) -> Source:
    client = httpx.Client(headers={"User-Agent": "gridcase"})
    if name == "eia":
        from gridcase.ingest.eia import EiaSource

        return EiaSource(client=client, api_key=config.eia_api_key(), raw_dir=config.RAW_DIR)
    if name == "openmeteo":
        from gridcase.ingest.openmeteo import OpenMeteoSource

        return OpenMeteoSource(client=client, raw_dir=config.RAW_DIR, live=live)
    if name == "gridstatus":
        from gridcase.ingest.gridstatus_src import GridstatusSource

        return GridstatusSource(raw_dir=config.RAW_DIR)
    raise SystemExit(f"unknown source {name}")


def cmd_ingest(args: argparse.Namespace) -> int:
    mode: Mode = "live" if args.live else "backfill"
    now = datetime.now(UTC).replace(tzinfo=None)
    start, end = live_window(now) if args.live else resolve_window(args.start, args.end, now)
    names = SOURCES if args.source == "all" else [args.source]
    conn = connect(config.DB_PATH)
    rc = 0
    for name in names:
        run_id = run_ingest(conn, build_source(name, args.live), start, end, mode)
        status, rows, err = one(
            conn, "SELECT status, rows_written, error FROM ingest_runs WHERE run_id = ?", [run_id]
        )
        print(f"{name}: {status}, {rows} new rows, run {run_id}" + (f" — {err}" if err else ""))
        rc = rc or (1 if status == "failed" else 0)
    conn.close()
    return rc


def cmd_replay(args: argparse.Namespace) -> int:
    import yaml

    from gridcase.investigate import investigate, recheck
    from gridcase.planner.llm import AnthropicLLM, FakeLLM

    spec = yaml.safe_load((config.ROOT / "bench" / "replay" / f"{args.case}.yaml").read_text())
    as_of = _parse_date(args.as_of) if args.as_of else spec["as_of"]
    llm = AnthropicLLM(model=args.model) if args.llm == "anthropic" else FakeLLM()
    conn = connect(config.DB_PATH)
    case = investigate(conn, llm, entity=spec["entity"], metric=spec["metric"], as_of=as_of)
    assert case.conclusion is not None
    print(
        f"case {case.case_id} [{case.state}] top={case.conclusion.top_hypothesis} "
        f"plan_attempts={case.plan_attempts} llm_calls={case.llm_calls} "
        f"cost=${case.llm_usage.cost_usd:.4f}"
    )
    for v in case.conclusion.ranked:
        print(f"  {v.id:28s} {v.status:9s} conf={v.confidence:.2f} margin={v.margin}")
    for c in case.conclusion.claims:
        print(f"  [{c.tier}] {c.text}  <- {','.join(c.receipt_ids) or '-'}")
    for cv in case.conclusion.caveats:
        print(f"  ! {cv}")
    if args.recheck:
        r = recheck(
            conn,
            case.case_id,
            _parse_date(args.recheck) if args.recheck != "spec" else spec["recheck_as_of"],
        )
        print("recheck:", r.verdicts)
    conn.close()
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from pathlib import Path

    from gridcase.bench.readme import render_readme
    from gridcase.bench.run import run_suite
    from gridcase.planner.llm import AnthropicLLM, FakeLLM

    fixture_parquet = config.ROOT / "tests" / "fixtures" / "store" / "observations.parquet"
    model = args.model or (config.PLANNER_MODEL if args.llm == "anthropic" else "fake")
    factory = (lambda: AnthropicLLM(model=model)) if args.llm == "anthropic" else FakeLLM
    work = Path(args.work_dir) if args.work_dir else config.DATA_DIR / "bench_work"
    source_db = config.DB_PATH if args.source == "store" else None
    result = run_suite(
        suite=args.suite,
        work_dir=work,
        llm_factory=factory,
        model_name=model,
        source_parquet=None if source_db else fixture_parquet,
        source_db=source_db,
        per_generator=args.per_generator,
        null_windows=args.null_windows,
        seed0=args.seed,
        validator=not args.no_validator,
    )
    out_dir = config.ROOT / "bench" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{datetime.now(UTC):%Y-%m-%d}_{model}" + ("_novalidator" if args.no_validator else "")
    (out_dir / f"{tag}.json").write_text(result.model_dump_json(indent=1))
    render_readme(out_dir, config.ROOT / "bench" / "README.md")
    s = result.summary
    print(
        f"{tag}: synthetic top-1 {s['synthetic']['top1']} top-3 {s['synthetic']['top3']} "
        f"(n={s['synthetic']['n']}), replay top-1 {s['replay']['top1']} (n={s['replay']['n']}), "
        f"false alarms {s['null']['false_alarm_rate']} (n={s['null']['n']})"
    )
    return 0


def cmd_init(_: argparse.Namespace) -> int:
    conn = connect(config.DB_PATH)
    n_series, n_ent = one(
        conn, "SELECT (SELECT count(*) FROM series), (SELECT count(*) FROM entities)"
    )
    print(f"{config.DB_PATH}: {n_series} series, {n_ent} entities")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="gridcase")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the store and seed series/entities").set_defaults(
        fn=cmd_init
    )
    ing = sub.add_parser("ingest", help="pull a source into the store")
    ing.add_argument("--source", choices=[*SOURCES, "all"], required=True)
    ing.add_argument("--start", help="YYYY-MM-DD[THH:MM] UTC; default 2018-07-01")
    ing.add_argument("--end", help="YYYY-MM-DD[THH:MM] UTC exclusive; default now")
    ing.add_argument(
        "--live", action="store_true", help="hourly mode: last 3 days to +2 days, real vintages"
    )
    ing.set_defaults(fn=cmd_ingest)
    rep = sub.add_parser("replay", help="replay a bench/replay case")
    rep.add_argument("--case", required=True, help="case id, e.g. uri_2021")
    rep.add_argument("--as-of", help="override the case's as_of (UTC)")
    rep.add_argument("--llm", choices=["fake", "anthropic"], default="fake")
    rep.add_argument("--model", help="model id for --llm anthropic; default from config")
    rep.add_argument("--recheck", help="'spec' to use the case's recheck_as_of, or a UTC datetime")
    rep.set_defaults(fn=cmd_replay)
    ben = sub.add_parser("bench", help="run the benchmark and regenerate bench/README.md")
    ben.add_argument("--suite", choices=["replay", "synthetic", "null", "all"], default="all")
    ben.add_argument("--llm", choices=["fake", "anthropic"], default="fake")
    ben.add_argument("--model", help="model id for --llm anthropic")
    ben.add_argument(
        "--source",
        choices=["fixture", "store"],
        default="store",
        help="scratch stores from the committed fixture or the full store",
    )
    ben.add_argument("--per-generator", type=int, default=25)
    ben.add_argument("--null-windows", type=int, default=100)
    ben.add_argument("--seed", type=int, default=1)
    ben.add_argument("--no-validator", action="store_true", help="ablation: skip plan validation")
    ben.add_argument("--work-dir", help="where scratch stores are built (default data/bench_work)")
    ben.set_defaults(fn=cmd_bench)
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
