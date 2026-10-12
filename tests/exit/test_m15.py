"""M15 exit: backfill, point-in-time history tools, a frozen eval set, and RL training.

Plan: docs/plans/2026-10-12_rl_backfill_eval_train.md. Each phase's contract is a section
below, written before its code.

Phase A, the spend ledger. Every paid model call (frontier evals, small-model evals, training)
reserves its cost first. The ledger is append-only JSONL, so it survives restarts; it refuses any
reservation that would pass the phase's allowance or the global cap ($150)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gridcase.rl.spend import ALLOWANCES, GLOBAL_CAP_USD, OverBudget, Spend

# ---------- phase A: spend ledger ----------


def test_allowances_sum_to_the_cap() -> None:
    assert GLOBAL_CAP_USD == 150.0
    assert sum(ALLOWANCES.values()) == pytest.approx(GLOBAL_CAP_USD)
    assert set(ALLOWANCES) == {"frontier_eval", "small_eval", "train", "reserve"}


def test_ledger_records_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "spend.jsonl"
    s = Spend(path)
    s.record("small_eval", "qwen", 1000, 200, 0.25)
    s.record("frontier_eval", "claude-sonnet-5-5", 5000, 900, 1.5)
    again = Spend(path)
    assert again.spent() == pytest.approx(1.75)
    assert again.spent("small_eval") == pytest.approx(0.25)
    assert len(path.read_text().splitlines()) == 2


def test_reserve_refuses_past_phase_allowance(tmp_path: Path) -> None:
    s = Spend(tmp_path / "spend.jsonl")
    s.record("small_eval", "qwen", 0, 0, ALLOWANCES["small_eval"] - 1.0)
    s.reserve("small_eval", 0.99)  # fits
    with pytest.raises(OverBudget):
        s.reserve("small_eval", 1.01)


def test_reserve_refuses_past_global_cap_even_with_phase_room(tmp_path: Path) -> None:
    s = Spend(tmp_path / "spend.jsonl", cap=10.0)
    s.record("train", "qwen", 0, 0, 9.5)
    with pytest.raises(OverBudget):
        s.reserve("frontier_eval", 1.0)


def test_unknown_phase_is_refused(tmp_path: Path) -> None:
    s = Spend(tmp_path / "spend.jsonl")
    with pytest.raises(KeyError):
        s.reserve("party", 1.0)
