"""M9 exit: a dbt project models the raw Polymarket trades, and agrees with the semantic layer.

dbt reads the collector's raw files (the real raw layer) and the server's market export, and
builds staging -> facts and dimensions -> a daily metrics mart in its own DuckDB warehouse.
On one real hour of raw trades: every dbt test passes; dbt keeps exactly the trades the app's
loader keeps; every daily metric equals the governed KPI from ontology/kpis.yaml (the single
place a KPI's meaning is defined); incremental runs add new trades once and duplicates never."""

from __future__ import annotations

import gzip
import json
import shutil
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from gridcase.config import ROOT
from gridcase.ingest.export import export_markets
from gridcase.ingest.polymarket_load import load_raw
from gridcase.semantic.kpis import KpiQuery, load_kpis, run_kpi
from gridcase.store.db import connect
from tests.polymarket_synth import add_market

FIXTURE = ROOT / "tests" / "fixtures" / "polymarket"
DBT_DIR = ROOT / "dbt"
DAY = datetime(2026, 9, 29)
LATER = datetime(2026, 10, 2)  # as_of after everything in the fixture was collected
METRICS = [  # metrics_daily column = governed KPI id
    "volume_usd",
    "trades",
    "active_traders",
    "new_traders",
    "markets_traded",
    "avg_trade_usd",
    "trades_per_trader",
    "whale_share",
    "sure_thing_share",
    "buy_share",
    "longshot_share",
    "recurring_share",
    "returning_share",
]


def dbt(*args: str) -> Any:
    from dbt.cli.main import dbtRunner

    res = dbtRunner().invoke(
        [*args, "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR), "--quiet"]
    )
    results: Any = res.result or []  # a RunExecutionResult: one entry per node
    failed = [(r.node.name, r.status, r.message) for r in results if r.status != "pass"]
    assert res.success, res.exception or failed
    return res


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Path]]:
    tmp = tmp_path_factory.mktemp("m9")
    raw = tmp / "raw"
    shutil.copytree(FIXTURE, raw)
    app = connect(tmp / "app.duckdb")
    load_raw(app, raw)
    top = app.execute(
        "SELECT condition_id FROM trades GROUP BY 1 ORDER BY sum(usd) DESC LIMIT 2"
    ).fetchall()
    add_market(app, top[0][0], "Sports", "Big match")
    add_market(app, top[1][0], "Crypto", "Big coin")
    export_markets(app, tmp / "exports" / "markets.parquet")
    app.close()
    mp = pytest.MonkeyPatch()
    mp.setenv("MARKETCASE_RAW", str(raw))
    mp.setenv("MARKETCASE_EXPORTS", str(tmp / "exports"))
    mp.setenv("MARKETCASE_WAREHOUSE", str(tmp / "warehouse.duckdb"))
    dbt("build")
    yield {"raw": raw, "app": tmp / "app.duckdb", "warehouse": tmp / "warehouse.duckdb"}
    mp.undo()


def wh(env: dict[str, Path]) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(env["warehouse"]))  # same settings as dbt's open connection


def test_1_dbt_keeps_exactly_the_trades_the_loader_keeps(env: dict[str, Path]) -> None:
    app = duckdb.connect(str(env["app"]), read_only=True)
    want = {r[0] for r in app.execute("SELECT trade_id FROM trades").fetchall()}
    app.close()
    w = wh(env)
    got = {r[0] for r in w.execute("SELECT trade_id FROM main_marts.fct_trades").fetchall()}
    w.close()
    assert len(want) > 10_000 and got == want


def test_2_every_daily_metric_equals_the_governed_kpi(env: dict[str, Path]) -> None:
    kpis, _ = load_kpis()
    assert set(METRICS) <= set(kpis)  # the mart's names are the semantic layer's ids
    w = wh(env)
    row = w.execute(
        f"SELECT {', '.join(METRICS)} FROM main_marts.metrics_daily WHERE day = ?", [DAY.date()]
    ).fetchone()
    w.close()
    assert row is not None
    app = connect(env["app"])
    for kpi, got in zip(METRICS, row, strict=True):
        q = KpiQuery(kpi=kpi, grain="day", window=(DAY, DAY + timedelta(days=1)), as_of=LATER)
        ((_, want),) = run_kpi(app, q)
        assert got == pytest.approx(want, rel=1e-9), kpi
    app.close()


def test_3_markets_and_wallets_are_dimensions(env: dict[str, Path]) -> None:
    w = wh(env)
    cats = dict(
        w.execute("SELECT category, count(*) FROM main_marts.dim_markets GROUP BY 1").fetchall()
    )
    kinds = w.execute(
        "SELECT DISTINCT market_kind FROM main_marts.dim_markets WHERE title ILIKE '%up or down%'"
    ).fetchall()
    (n_wallets,) = w.execute("SELECT count(*) FROM main_marts.dim_wallets").fetchone() or (0,)
    (active,) = w.execute(
        "SELECT count(DISTINCT wallet) FROM main_marts.fct_trades"
    ).fetchone() or (0,)
    w.close()
    assert cats.get("Sports") == 1 and cats.get("Crypto") == 1 and cats.get("Other", 0) > 100
    assert kinds == [("recurring",)]
    assert n_wallets == active


def _add_raw(env: dict[str, Path], name: str, rows: list[dict[str, Any]]) -> None:
    path = env["raw"] / "trades" / "2026" / "09" / "29" / name
    with gzip.open(path, "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_4_incremental_runs_add_new_trades_once_and_duplicates_never(
    env: dict[str, Path],
) -> None:
    w = wh(env)
    (before,) = w.execute("SELECT count(*) FROM main_marts.fct_trades").fetchone() or (0,)
    w.close()
    src = sorted((FIXTURE / "trades").rglob("*.jsonl.gz"))[0]
    with gzip.open(src, "rt") as f:
        known = [json.loads(line) for _, line in zip(range(50), f, strict=False)]
    _add_raw(env, "20.restart-copy.jsonl.gz", known)  # a collector restart re-saves trades
    fresh = [
        {**known[0], "transactionHash": f"0xnew{i}", "collected_at": "2026-09-29T20:59:00+00:00"}
        for i in range(2)
    ]
    _add_raw(env, "20.jsonl.gz", fresh)
    dbt("build")
    dbt("build")  # running again changes nothing
    w = wh(env)
    (after,) = w.execute("SELECT count(*) FROM main_marts.fct_trades").fetchone() or (0,)
    w.close()
    assert after == before + 2


def test_5_the_cli_runs_dbt_with_the_projects_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gridcase dbt <args>` points dbt at our raw files, exports and warehouse."""
    from gridcase import cli

    seen: dict[str, Any] = {}

    def fake_invoke(args: list[str]) -> int:
        import os

        seen["args"] = args
        seen["env"] = {k: os.environ[k] for k in os.environ if k.startswith("MARKETCASE_")}
        return 0

    monkeypatch.setattr(cli, "run_dbt", fake_invoke)
    assert cli.main(["dbt", "build", "--select", "metrics_daily"]) == 0
    assert seen["args"][:3] == ["build", "--select", "metrics_daily"]
    assert {"MARKETCASE_RAW", "MARKETCASE_EXPORTS", "MARKETCASE_WAREHOUSE"} <= set(seen["env"])


def test_6_the_dbt_docs_are_served_with_the_app(tmp_path: Path, market_store: Path) -> None:
    from fastapi.testclient import TestClient

    from gridcase.planner.llm import FakeLLM
    from gridcase.server.app import create_app

    (tmp_path / "index.html").write_text("<html>dbt docs</html>")
    app = create_app(
        db_path=market_store,
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        dbt_docs_dir=tmp_path,
    )
    with TestClient(app) as c:
        assert "dbt docs" in c.get("/marketcase/dbt/").text
        assert "dbt docs" in c.get("/dbt/").text
