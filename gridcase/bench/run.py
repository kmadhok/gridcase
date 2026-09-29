"""Run the replay, synthetic and null suites; every case is scored, never raised.

Each case runs on its own scratch store copied from one base scratch, so nothing leaks
between cases (no learned priors, no ledger) and the measurement is of the system without
memory. The validator-off ablation disables plan validation in the planner for the run.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from gridcase.bench.inject import (
    GENERATORS,
    Scenario,
    build_scratch,
    copy_scratch,
    inject,
    sample_null,
    sample_scenario,
)
from gridcase.config import ROOT
from gridcase.investigate import investigate, recheck
from gridcase.planner import planner as planner_mod
from gridcase.planner.llm import LLM
from gridcase.store.db import connect
from gridcase.tools.base import run_tool

log = logging.getLogger("gridcase")


class CaseResult(BaseModel):
    case_id: str
    scenario_id: str
    generator: str | None
    entity: str
    as_of: datetime
    ground_truth: list[str]
    predicted: list[str]
    confidences: dict[str, float] = Field(default_factory=dict)
    flagged: bool
    top1: bool
    top3: bool
    brier: float
    receipt_coverage: float
    plan_h0_first: bool
    n_hypotheses: int
    steps_run: int
    steps_skipped: int
    llm_calls: int
    cost_usd: float
    latency_s: float
    error: str | None = None
    recheck: dict[str, str] | None = None


class SuiteResult(BaseModel):
    config: dict[str, Any]
    replay: list[CaseResult] = Field(default_factory=list)
    synthetic: list[CaseResult] = Field(default_factory=list)
    null: list[CaseResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


def load_replay_cases() -> list[dict[str, Any]]:
    out = []
    for p in sorted((ROOT / "bench" / "replay").glob("*.yaml")):
        out.append(yaml.safe_load(p.read_text()))
    return out


def _miss(sc: Scenario, error: str, t0: float) -> CaseResult:
    return CaseResult(
        case_id="",
        scenario_id=sc.id,
        generator=sc.generator,
        entity=sc.entity,
        as_of=sc.as_of,
        ground_truth=sc.ground_truth,
        predicted=[],
        flagged=False,
        top1=False,
        top3=False,
        brier=1.0,
        receipt_coverage=0.0,
        plan_h0_first=False,
        n_hypotheses=0,
        steps_run=0,
        steps_skipped=0,
        llm_calls=0,
        cost_usd=0.0,
        latency_s=time.perf_counter() - t0,
        error=error[:500],
    )


def run_case(
    db_path: Path, sc: Scenario, llm: LLM, recheck_as_of: datetime | None = None
) -> CaseResult:
    t0 = time.perf_counter()
    conn = None
    try:
        conn = connect(db_path)
        inject(conn, sc)
        case = investigate(conn, llm, entity=sc.entity, metric=sc.metric, as_of=sc.as_of)
        assert case.conclusion is not None and case.plan is not None
        first = next(r for r in case.receipts if r.tool == "anomaly.detect")
        flagged = bool(first.output.get("flagged"))
        ranked = case.conclusion.ranked
        predicted = [v.id for v in ranked if v.status != "refuted"] + [
            v.id for v in ranked if v.status == "refuted"
        ]
        conf = {v.id: round(v.confidence, 4) for v in ranked}
        truth_top = sc.ground_truth[0] if sc.ground_truth else None
        top1 = bool(predicted) and predicted[0] == truth_top
        if not sc.expect_flagged:
            top1 = top1 or not flagged
        top3 = truth_top in predicted[:3] if truth_top else not flagged
        p_top = conf.get(predicted[0], 0.0) if predicted else 0.0
        brier = (p_top - (1.0 if top1 else 0.0)) ** 2
        cited = [c for c in case.conclusion.claims if c.tier != "inferred"]
        coverage = (sum(1 for c in cited if c.receipt_ids) / len(cited)) if cited else 1.0
        steps = case.plan.steps
        run_ids = {r.receipt_id for r in case.receipts}
        ver: dict[str, str] | None = None
        if recheck_as_of is not None:
            ver = recheck(conn, case.case_id, recheck_as_of).verdicts
        result = CaseResult(
            case_id=case.case_id,
            scenario_id=sc.id,
            generator=sc.generator,
            entity=sc.entity,
            as_of=sc.as_of,
            ground_truth=sc.ground_truth,
            predicted=predicted,
            confidences=conf,
            flagged=flagged,
            top1=top1,
            top3=top3,
            brier=brier,
            receipt_coverage=coverage,
            plan_h0_first=bool(case.plan.hypotheses)
            and case.plan.hypotheses[0] == "H0_DATA_ARTIFACT",
            n_hypotheses=len(case.plan.hypotheses),
            steps_run=min(len(steps), len(run_ids) - 1) if run_ids else 0,
            steps_skipped=max(0, len(steps) - (len(run_ids) - 1)),
            llm_calls=case.llm_calls,
            cost_usd=case.llm_usage.cost_usd,
            latency_s=time.perf_counter() - t0,
            recheck=ver,
        )
        return result
    except Exception as e:  # noqa: BLE001 - a failure is a scored miss, by design
        log.warning("case %s failed: %s", sc.id, e)
        return _miss(sc, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}", t0)
    finally:
        if conn is not None:
            conn.close()


def run_null(db_path: Path, sc: Scenario) -> CaseResult:
    t0 = time.perf_counter()
    try:
        conn = connect(db_path, read_only=True)
        try:
            out = run_tool(
                conn,
                "anomaly.detect",
                {"metric": sc.metric, "entity": sc.entity, "window": sc.window},
                sc.as_of,
            ).output
        finally:
            conn.close()
        flagged = bool(out["flagged"])
        return CaseResult(
            case_id="",
            scenario_id=sc.id,
            generator=None,
            entity=sc.entity,
            as_of=sc.as_of,
            ground_truth=[],
            predicted=[],
            flagged=flagged,
            top1=not flagged,
            top3=not flagged,
            brier=0.0,
            receipt_coverage=1.0,
            plan_h0_first=True,
            n_hypotheses=0,
            steps_run=1,
            steps_skipped=0,
            llm_calls=0,
            cost_usd=0.0,
            latency_s=time.perf_counter() - t0,
        )
    except Exception as e:  # noqa: BLE001
        return _miss(sc, f"{type(e).__name__}: {e}", t0)


def run_suite(
    suite: str,
    work_dir: Path,
    llm_factory: Callable[[], LLM],
    model_name: str,
    source_parquet: Path | None = None,
    source_db: Path | None = None,
    per_generator: int = 25,
    null_windows: int = 100,
    seed0: int = 1,
    validator: bool = True,
    entities: tuple[str, ...] = ("ERCO",),
) -> SuiteResult:
    work_dir.mkdir(parents=True, exist_ok=True)
    base = work_dir / "base.duckdb"
    build_scratch(source_parquet, base, source_db=source_db)
    result = SuiteResult(
        config={
            "suite": suite,
            "model": model_name,
            "validator": validator,
            "seed0": seed0,
            "per_generator": per_generator,
            "null_windows": null_windows,
            "entities": list(entities),
            "source": str(source_db or source_parquet),
            "started_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
    )
    planner_mod.set_validation(validator)
    try:
        scratch = work_dir / "case.duckdb"
        if suite in ("replay", "all"):
            for spec in load_replay_cases():
                sc = Scenario(
                    id=spec["id"],
                    generator=None,
                    seed=0,
                    entity=spec["entity"],
                    metric=spec["metric"],
                    as_of=spec["as_of"],
                    window=(spec["as_of"], spec["as_of"]),
                    fault=None,
                    ground_truth=list(spec["ground_truth"]["ranked"]),
                    expect_flagged=spec.get("expect_flagged", True),
                )
                copy_scratch(base, scratch)
                result.replay.append(
                    run_case(scratch, sc, llm_factory(), recheck_as_of=spec.get("recheck_as_of"))
                )
        if suite in ("synthetic", "all"):
            for entity in entities:
                for gen in GENERATORS:
                    for i in range(per_generator):
                        sc = sample_scenario(gen, seed=seed0 + i, entity=entity)
                        copy_scratch(base, scratch)
                        result.synthetic.append(run_case(scratch, sc, llm_factory()))
        if suite in ("null", "all"):
            for entity in entities:
                for i in range(null_windows):
                    result.null.append(run_null(base, sample_null(seed0 + i, entity)))
    finally:
        planner_mod.set_validation(True)
    from gridcase.bench.score import score

    result.summary = score(result)
    result.config["finished_at"] = datetime.utcnow().isoformat(timespec="seconds")
    return result
