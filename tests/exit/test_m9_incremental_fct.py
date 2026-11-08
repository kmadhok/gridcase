"""Synthetic fact parity and write selection, independent of live files or databases."""

from __future__ import annotations

import gzip
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from gridcase.config import ROOT

# Freeze the pre-incremental projection as an independent full-refresh oracle. In particular,
# the reference never uses the incremental model's comparison/filter, even on later builds.
FULL_FACT_SQL = """
select t.trade_id, t.ts, cast(t.ts as date) as day, date_trunc('hour', t.ts) as hour,
       t.wallet, t.condition_id, t.asset, t.outcome, lower(t.side) as side,
       t.size, t.price, t.usd, m.category, m.market_name, m.market_kind,
       case when t.usd < 10 then 'under $10' when t.usd < 100 then '$10-100'
            when t.usd < 1000 then '$100-1k' else '$1k+' end as size_band,
       case when t.price < 0.2 then 'longshot' when t.price > 0.8 then 'favourite'
            else 'toss-up' end as odds_band,
       w.first_day = cast(t.ts as date) as is_new_trader, t.collected_at
from {{ ref('stg_polymarket__trades') }} t
join {{ ref('dim_wallets') }} w using (wallet)
join {{ ref('dim_markets') }} m using (condition_id)
"""


def _raw(
    key: str,
    wallet: str,
    market: str,
    day: str,
    *,
    collected: str = "2026-10-15T12:00:00+00:00",
    title: str = "Will it happen?",
    size: float = 20.0,
    price: float = 0.5,
) -> dict[str, Any]:
    return {
        "transactionHash": key,
        "proxyWallet": wallet,
        "conditionId": market,
        "asset": market + "-yes",
        "side": "BUY",
        "size": size,
        "price": price,
        "timestamp": int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()),
        "title": title,
        "slug": market,
        "eventSlug": "",
        "outcome": None if market == "m2" else "Yes",
        "collected_at": collected,
    }


class WarehousePair:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp
        self.monkeypatch = monkeypatch
        self.raw = tmp / "raw"
        self.exports = tmp / "exports"
        self.exports.mkdir()
        self.project = tmp / "project"
        self.reference = tmp / "reference"
        for project in (self.project, self.reference):
            shutil.copytree(
                ROOT / "dbt", project, ignore=shutil.ignore_patterns("target", "logs", ".user.yml")
            )
        (self.reference / "models/marts/fct_trades.sql").write_text(FULL_FACT_SQL)
        self.warehouse = tmp / "incremental.duckdb"
        self.oracle = tmp / "full.duckdb"
        self.markets: list[tuple[str, str | None, str | None]] = [
            ("m1", "Sports", "Match"),
            ("m2", "Crypto", "Coin"),
        ]
        self.export()

    def append(self, name: str, rows: list[dict[str, Any]]) -> None:
        # Collection-hour paths advance while event timestamps can go arbitrarily far back.
        path = self.raw / "trades/2026/10" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def export(self) -> None:
        with duckdb.connect() as conn:
            conn.execute(
                "CREATE TABLE markets (condition_id VARCHAR, category VARCHAR, "
                "event_title VARCHAR, question VARCHAR, event_slug VARCHAR, tags VARCHAR, "
                "fetched_at TIMESTAMP)"
            )
            conn.executemany(
                "INSERT INTO markets VALUES (?, ?, ?, 'Question', '', '', '2026-10-15')",
                self.markets,
            )
            conn.execute(
                "COPY markets TO ? (FORMAT PARQUET)", [str(self.exports / "markets.parquet")]
            )

    def dbt(
        self, command: str, *selection: str, reference: bool = False, full: bool = False
    ) -> None:
        from dbt.cli.main import dbtRunner

        project = self.reference if reference else self.project
        self.monkeypatch.setenv("MARKETCASE_RAW", str(self.raw))
        self.monkeypatch.setenv("MARKETCASE_EXPORTS", str(self.exports))
        self.monkeypatch.setenv(
            "MARKETCASE_WAREHOUSE", str(self.oracle if reference else self.warehouse)
        )
        args = [
            command,
            *selection,
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(project),
            "--quiet",
            "--no-send-anonymous-usage-stats",
        ]
        if full:
            args.append("--full-refresh")
        if command == "compile":
            args.extend(["--select", "fct_trades"])
        result = dbtRunner().invoke(args)
        assert result.success, result.exception or str(result.result)

    def candidates(self) -> set[str]:
        """Run dbt's actual pre-write SELECT: a no-op must offer zero rows to the strategy."""
        self.dbt("compile")
        compiled = self.project / "target/compiled/marketcase/models/marts/fct_trades.sql"
        with duckdb.connect(str(self.warehouse)) as conn:
            rows = conn.execute(compiled.read_text()).fetchall()
        keys = [row[0] for row in rows]
        assert len(keys) == len(set(keys))
        return set(keys)

    def facts(self) -> dict[str, tuple[Any, ...]]:
        with duckdb.connect(str(self.warehouse)) as conn:
            return {
                row[0]: row
                for row in conn.execute("SELECT * FROM main_marts.fct_trades").fetchall()
            }

    def keys(self, predicate: str) -> set[str]:
        with duckdb.connect(str(self.warehouse)) as conn:
            return {
                row[0]
                for row in conn.execute(
                    f"SELECT trade_id FROM main_staging.stg_polymarket__trades WHERE {predicate}"
                ).fetchall()
            }

    def parity(self) -> None:
        self.dbt("build", reference=True, full=True)
        with duckdb.connect(str(self.oracle)) as conn:
            expected = conn.execute(
                "SELECT * FROM main_marts.fct_trades ORDER BY trade_id"
            ).fetchall()
            schema = conn.execute("DESCRIBE main_marts.fct_trades").fetchall()
        with duckdb.connect(str(self.warehouse)) as conn:
            actual = conn.execute(
                "SELECT * FROM main_marts.fct_trades ORDER BY trade_id"
            ).fetchall()
            assert conn.execute("DESCRIBE main_marts.fct_trades").fetchall() == schema
        assert actual == expected

    def build(self, changed: set[str], added: int = 0) -> None:
        before = self.facts()
        # Build upstream only, just as the dependencies are resolved before fct_trades in a
        # normal build. Compile then inspect the exact batch before delete+insert executes.
        self.dbt("run", "--select", "+fct_trades", "--exclude", "fct_trades")
        candidates = self.candidates()
        assert candidates & before.keys() == changed
        assert len(candidates - before.keys()) == added
        self.dbt("build")
        assert len(self.facts()) == len(before) + added
        self.parity()


def test_incremental_facts_match_independent_full_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = WarehousePair(tmp_path, monkeypatch)
    initial = [
        _raw("a1", "alice", "m1", "2026-10-10T10:00", size=19, price=0.1),
        _raw("a2", "alice", "m1", "2026-10-10T11:00", size=200, price=0.5),
        _raw("a3", "alice", "m2", "2026-10-12T12:00", size=10000, price=0.9),
        _raw("b", "bob", "m2", "2026-10-11T12:00", size=100, price=0.2),
        _raw("c", "carol", "m3", "2026-10-12T12:00", price=0.8),
    ]
    pair.append("15/12.jsonl.gz", initial)
    pair.dbt("build")
    pair.parity()

    manifest = json.loads((pair.project / "target/manifest.json").read_text())
    config = manifest["nodes"]["model.marketcase.fct_trades"]["config"]
    assert (config["materialized"], config["unique_key"], config["incremental_strategy"]) == (
        "incremental",
        "trade_id",
        "delete+insert",
    )
    pair.build(set())  # An unchanged run selects nothing, including nullable outcomes.

    late = _raw("late", "dave", "m2", "2026-09-20T01:00", collected="2026-10-20T12:00:00Z")
    pair.append("20/12.jsonl.gz", [late])
    pair.build(set(), added=1)  # Event time is older than every stored fact.

    pair.append(
        "21/12.jsonl.gz",
        [{**row, "collected_at": "2026-10-21T12:00:00Z"} for row in [*initial, late]],
    )
    pair.build(set())  # Duplicate collector copies preserve earliest collected_at.
    pair.build(set())  # An ordinary rerun stays empty.

    alice_first_day = pair.keys("wallet = 'alice' AND cast(ts AS DATE) = '2026-10-10'")
    pair.append(
        "22/12.jsonl.gz",
        [_raw("earlier", "alice", "m2", "2026-09-19T01:00", collected="2026-10-22T12:00:00Z")],
    )
    pair.build(alice_first_day, added=1)

    m1 = pair.keys("condition_id = 'm1'")
    pair.markets[0] = ("m1", "Politics", "Match")
    pair.export()
    pair.build(m1)  # Historical category correction, with no new raw trades.
    pair.markets[0] = ("m1", "Politics", "Corrected match")
    pair.export()
    pair.build(m1)  # Historical name correction alone.
    pair.markets[0] = ("m1", None, None)
    pair.export()
    pair.build(m1)  # Metadata removal restores Other and the raw-title fallback.

    pair.append(
        "23/12.jsonl.gz",
        [
            _raw(
                "kind",
                "eve",
                "m1",
                "2026-09-21T01:00",
                title="Z Up or Down?",
                collected="2026-10-23T12:00:00Z",
            )
        ],
    )
    pair.build(m1, added=1)  # The resolved title/kind of existing facts changes too.

    # A missing historical fact is repaired even though staging has no new input.
    missing = pair.keys("wallet = 'carol'")
    with duckdb.connect(str(pair.warehouse)) as conn:
        conn.execute("DELETE FROM main_marts.fct_trades WHERE wallet = 'carol'")
    pair.build(set(), added=1)
    assert missing <= pair.facts().keys()
    pair.build(set())

    # Explicit recovery still takes the full-refresh path and retains the original schema.
    pair.dbt("build", full=True)
    pair.parity()
