"""Scoring: top-1/top-3, false alarms, Brier, receipt coverage, trajectory, cost."""

from __future__ import annotations

from datetime import datetime

import pytest

from gridcase.bench.run import CaseResult, SuiteResult
from gridcase.bench.score import score


def case(**over: object) -> CaseResult:
    base: dict[str, object] = {
        "case_id": "c",
        "scenario_id": "s",
        "generator": "H2_SUPPLY_SHORTFALL",
        "entity": "ERCO",
        "as_of": datetime(2023, 5, 3),
        "ground_truth": ["H2_SUPPLY_SHORTFALL", "H4_LOAD_SHED"],
        "predicted": ["H2_SUPPLY_SHORTFALL", "H4_LOAD_SHED", "H3_IMPORT_CONSTRAINT"],
        "confidences": {"H2_SUPPLY_SHORTFALL": 0.8, "H4_LOAD_SHED": 0.5},
        "flagged": True,
        "top1": True,
        "top3": True,
        "brier": 0.04,
        "receipt_coverage": 1.0,
        "plan_h0_first": True,
        "n_hypotheses": 8,
        "steps_run": 12,
        "steps_skipped": 0,
        "llm_calls": 2,
        "cost_usd": 0.0,
        "latency_s": 3.2,
        "error": None,
    }
    base.update(over)
    return CaseResult(**base)  # type: ignore[arg-type]


def suite(**over: object) -> SuiteResult:
    base: dict[str, object] = {
        "config": {"model": "fake", "validator": True, "suite": "all", "seed0": 1},
        "replay": [],
        "synthetic": [],
        "null": [],
        "summary": {},
    }
    base.update(over)
    return SuiteResult(**base)  # type: ignore[arg-type]


def test_top1_top3_and_brier_over_synthetic_cases() -> None:
    s = score(
        suite(
            synthetic=[
                case(),
                case(
                    predicted=["H4_LOAD_SHED", "H2_SUPPLY_SHORTFALL"],
                    top1=False,
                    top3=True,
                    brier=0.25,
                    confidences={"H4_LOAD_SHED": 0.5},
                ),
                case(predicted=["H7_CALENDAR"], top1=False, top3=False, brier=0.49),
                case(error="boom", predicted=[], top1=False, top3=False, brier=1.0),
            ]
        )
    )
    assert s["synthetic"]["n"] == 4
    assert s["synthetic"]["top1"] == pytest.approx(0.25)
    assert s["synthetic"]["top3"] == pytest.approx(0.5)
    assert s["synthetic"]["brier"] == pytest.approx((0.04 + 0.25 + 0.49 + 1.0) / 4)
    assert s["synthetic"]["failures"] == 1


def test_false_alarm_rate_is_the_flagged_share_of_null_windows() -> None:
    s = score(
        suite(
            null=[
                case(generator=None, ground_truth=[], flagged=f)
                for f in (True, False, False, False)
            ]
        )
    )
    assert s["null"]["false_alarm_rate"] == pytest.approx(0.25)


def test_per_hypothesis_and_per_region_breakdowns() -> None:
    s = score(
        suite(
            synthetic=[
                case(),
                case(
                    generator="H1_WEATHER_DEMAND",
                    ground_truth=["H1_WEATHER_DEMAND"],
                    top1=False,
                    top3=True,
                ),
                case(entity="PJM", top1=False, top3=False),
            ]
        )
    )
    assert s["synthetic"]["per_hypothesis"]["H2_SUPPLY_SHORTFALL"]["top1"] == pytest.approx(0.5)
    assert s["synthetic"]["per_hypothesis"]["H1_WEATHER_DEMAND"]["n"] == 1
    assert s["synthetic"]["per_region"]["PJM"]["top1"] == 0.0
    assert s["synthetic"]["per_region"]["ERCO"]["n"] == 2


def test_trajectory_and_cost_summaries() -> None:
    s = score(
        suite(
            synthetic=[
                case(),
                case(
                    plan_h0_first=False,
                    n_hypotheses=1,
                    steps_run=6,
                    steps_skipped=2,
                    cost_usd=0.1,
                    latency_s=8,
                ),
            ]
        )
    )
    t = s["synthetic"]["trajectory"]
    assert t["h0_first_rate"] == pytest.approx(0.5) and t[
        "two_plus_hypotheses_rate"
    ] == pytest.approx(0.5)
    assert t["mean_steps"] == pytest.approx(9) and t["mean_skipped"] == pytest.approx(1)
    assert s["synthetic"]["cost_usd_per_case"] == pytest.approx(0.05)
    assert s["synthetic"]["latency_s_per_case"] == pytest.approx(5.6)


def test_replay_cases_are_scored_separately_and_confusion_matrix_is_built() -> None:
    s = score(
        suite(
            replay=[
                case(scenario_id="uri_2021"),
                case(scenario_id="x", error="no data", predicted=[], top1=False, top3=False),
            ]
        )
    )
    assert s["replay"]["n"] == 2 and s["replay"]["top1"] == pytest.approx(0.5)
    s2 = score(suite(synthetic=[case(), case(predicted=["H4_LOAD_SHED"], top1=False)]))
    cm = s2["synthetic"]["confusion"]
    assert (
        cm["H2_SUPPLY_SHORTFALL"]["H2_SUPPLY_SHORTFALL"] == 1
        and cm["H2_SUPPLY_SHORTFALL"]["H4_LOAD_SHED"] == 1
    )


def test_empty_suites_do_not_divide_by_zero() -> None:
    s = score(suite())
    assert s["synthetic"]["n"] == 0 and s["null"]["false_alarm_rate"] is None
