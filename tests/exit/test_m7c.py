"""M7c exit: a KPI catalog a recruiter or PM can read.

`/api/catalog` lists every KPI a reader may meet (building blocks hidden) with its plain
definition, its formula in words, the governed definition behind it, whether it is watched
(and its status), and its value over the last complete hour."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gridcase.market_alarm import load_watch
from gridcase.planner.llm import FakeLLM
from gridcase.semantic.kpis import Measure, load_kpis
from gridcase.server.app import create_app
from tests.polymarket_synth import AS_OF


@pytest.fixture()
def catalog(market_store: Path) -> dict[str, Any]:
    app = create_app(
        db_path=market_store,
        token="t",
        llm_factory=FakeLLM,
        drain_interval_s=None,
        clock=lambda: AS_OF,
    )
    with TestClient(app) as client:
        r = client.get("/api/catalog")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def test_1_every_readable_kpi_is_listed_with_a_plain_definition(catalog: dict[str, Any]) -> None:
    kpis, _ = load_kpis()
    readable = {k for k, v in kpis.items() if not (isinstance(v, Measure) and v.internal)}
    listed = {k["id"]: k for k in catalog["kpis"]}
    assert set(listed) == readable
    assert "whale_volume" not in listed  # a building block, not a KPI
    for k in listed.values():
        assert k["label"] and k["description"] and k["formula"] and k["definition"]
        assert k["unit"] in {"usd", "count", "share", "ratio"}
    assert listed["whale_share"]["formula"] == "Whale volume ÷ Trading volume"
    assert "sum(usd)" in listed["volume_usd"]["definition"]


def test_2_watched_kpis_carry_their_status_and_every_kpi_its_last_hour(
    catalog: dict[str, Any],
) -> None:
    watched = {w.kpi for w in load_watch()[0] if not w.filters}
    listed = {k["id"]: k for k in catalog["kpis"]}
    for kid, k in listed.items():
        assert k["watched"] == (kid in watched)
        if k["watched"]:
            assert k["status"] in {"normal", "alarm", "learning"}
    assert catalog["hour"] == ["2026-10-10T13:00:00", "2026-10-10T14:00:00"]
    assert listed["volume_usd"]["value"] == pytest.approx(3_600, rel=0.1)
    assert listed["markets_traded"]["value"] == 6
    assert listed["volume_share"]["value"] == pytest.approx(1.0)


def test_3_dimensions_are_listed_for_slicing(catalog: dict[str, Any]) -> None:
    dims = {d["id"]: d for d in catalog["dimensions"]}
    assert {"category", "market", "size_band", "market_kind"} <= set(dims)
    assert dims["category"]["label"] == "Category" and "Sports" in dims["category"]["values"]


def test_4_formulas_name_each_part_once(catalog: dict[str, Any]) -> None:
    listed = {k["id"]: k for k in catalog["kpis"]}
    assert listed["longshot_share"]["formula"] == "Long-shot trades ÷ Trades"
    assert listed["returning_share"]["formula"] == "1 - New traders ÷ Active traders"
    assert listed["volume_usd"]["formula"] == "Added up over trades"
