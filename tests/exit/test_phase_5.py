"""Phase 5 exit test: a reproducible score for root-cause accuracy, false alarms and
calibration.

Offline: a reduced suite (2 synthetic cases per generator + 8 null windows + the 5 replay
cases, four of which have no data in the fixture and must be *scored* as misses, not raised).
The full 5 + 300 run is `live` on the VPS.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from gridcase.bench.inject import GENERATORS, build_scratch, inject, sample_scenario, store_digest
from gridcase.bench.readme import render_readme
from gridcase.bench.run import SuiteResult, run_suite
from gridcase.bench.score import score
from gridcase.planner.llm import FakeLLM
from tests.conftest import FIXTURE_PARQUET

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def suite_result(tmp_path_factory: pytest.TempPathFactory) -> SuiteResult:
    out = tmp_path_factory.mktemp("bench")
    return run_suite(
        suite="all",
        source_parquet=FIXTURE_PARQUET,
        work_dir=out,
        llm_factory=FakeLLM,
        model_name="fake",
        per_generator=2,
        null_windows=8,
        seed0=1,
    )


def test_1_every_case_finishes_and_failures_are_scored_misses(suite_result: SuiteResult) -> None:
    r = suite_result
    assert len(r.replay) == 5 and len(r.synthetic) == 2 * len(GENERATORS) and len(r.null) == 8
    for case in [*r.replay, *r.synthetic, *r.null]:
        assert case.error is None or case.top1 is False  # a failure is a scored miss
    # the four replay events without fixture data are recorded, not raised
    assert sum(1 for c in r.replay if c.error) >= 3
    assert any(c.scenario_id == "uri_2021" and c.error is None for c in r.replay)


def test_2_injection_is_reproducible_per_seed(tmp_path: Path) -> None:
    digests = []
    for i in range(2):
        path = tmp_path / f"s{i}.duckdb"
        build_scratch(FIXTURE_PARQUET, path)
        conn = duckdb.connect(str(path))
        sc = sample_scenario("H2_SUPPLY_SHORTFALL", seed=42, entity="ERCO")
        inject(conn, sc)
        digests.append(store_digest(conn))
        conn.close()
    assert digests[0] == digests[1]
    other = tmp_path / "s3.duckdb"
    build_scratch(FIXTURE_PARQUET, other)
    conn = duckdb.connect(str(other))
    inject(conn, sample_scenario("H2_SUPPLY_SHORTFALL", seed=43, entity="ERCO"))
    assert store_digest(conn) != digests[0]
    conn.close()


def test_3_accuracy_and_false_alarm_targets_are_reported(suite_result: SuiteResult) -> None:
    s = score(suite_result)
    assert 0.0 <= s["synthetic"]["top1"] <= 1.0 and 0.0 <= s["null"]["false_alarm_rate"] <= 1.0
    assert "brier" in s["synthetic"] and "receipt_coverage" in s["synthetic"]
    assert s["synthetic"]["receipt_coverage"] == pytest.approx(1.0)
    assert set(s["synthetic"]["per_hypothesis"]) == set(GENERATORS)
    # Targets are placeholders the human sets (spec); record, do not gate.
    s["targets"] = {"top1_min": 0.7, "false_alarm_max": 0.1}
    s["meets_targets"] = s["synthetic"]["top1"] >= 0.7 and s["null"]["false_alarm_rate"] <= 0.1
    assert isinstance(s["meets_targets"], bool)


def test_4_validator_off_ablation_runs_and_is_labelled(tmp_path: Path) -> None:
    r = run_suite(
        suite="synthetic",
        source_parquet=FIXTURE_PARQUET,
        work_dir=tmp_path,
        llm_factory=FakeLLM,
        model_name="fake",
        per_generator=1,
        validator=False,
        seed0=7,
    )
    assert r.config["validator"] is False and len(r.synthetic) == len(GENERATORS)


def test_5_readme_regenerates_from_results_json_with_one_command(
    suite_result: SuiteResult, tmp_path: Path
) -> None:
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / "2026-09-28_fake.json").write_text(suite_result.model_dump_json())
    out = tmp_path / "README.md"
    text1 = render_readme(results_dir, out)
    text2 = render_readme(results_dir, out)
    assert text1 == text2 and "fake" in text1 and "top-1" in text1.lower()
    cmd = [
        sys.executable,
        "-m",
        "gridcase.bench.readme",
        "--results",
        str(results_dir),
        "--out",
        str(out),
    ]
    assert subprocess.run(cmd, capture_output=True, cwd=ROOT).returncode == 0
    assert (
        hashlib.sha256(out.read_bytes()).hexdigest() == hashlib.sha256(text1.encode()).hexdigest()
    )


def test_6_results_json_is_self_describing(suite_result: SuiteResult) -> None:
    doc = json.loads(suite_result.model_dump_json())
    assert doc["config"]["model"] == "fake" and doc["config"]["validator"] is True
    assert {"replay", "synthetic", "null", "summary"} <= set(doc)


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("GRIDCASE_LIVE"), reason="full 5 + 300 suite on the VPS")
def test_7_full_suite_on_the_vps(tmp_path: Path) -> None:
    from gridcase.config import DB_PATH

    r = run_suite(
        suite="all",
        source_db=DB_PATH,
        work_dir=tmp_path,
        llm_factory=FakeLLM,
        model_name="fake",
        per_generator=25,
        null_windows=100,
        seed0=1,
    )
    assert len(r.synthetic) == 200 and len(r.null) == 100 and len(r.replay) == 5
