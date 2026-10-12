"""Paths, environment and constants. Secrets come only from .env via python-dotenv."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("GRIDCASE_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("GRIDCASE_DB", DATA_DIR / "gridcase.duckdb"))
RAW_DIR = DATA_DIR / "raw"
EXPORT_DIR = DATA_DIR / "exports"  # files the server writes for the dbt warehouse
WAREHOUSE_PATH = DATA_DIR / "warehouse.duckdb"  # dbt's own DuckDB file (dbt/)
DBT_DIR = ROOT / "dbt"
ONTOLOGY_DIR = ROOT / "ontology"


# How often the server loads newly collected trades into the store (seconds).
POLYMARKET_LOAD_EVERY_S = 300.0

# Models: names live in config, never in code. Prices are USD per million tokens (input, output).
PLANNER_MODEL = os.environ.get("GRIDCASE_PLANNER_MODEL", "claude-opus-5")
CHEAP_MODEL = os.environ.get("GRIDCASE_CHEAP_MODEL", "claude-sonnet-5")
ASK_MODEL = os.environ.get("GRIDCASE_ASK_MODEL", CHEAP_MODEL)
# the Explore chat (OpenRouter): picked by experiments/model_pick (DECISIONS 2026-10-05, -06)
CHAT_MODEL = os.environ.get("GRIDCASE_CHAT_MODEL", "google/gemini-3.1-flash-lite")
CHAT_FALLBACK_MODEL = os.environ.get("GRIDCASE_CHAT_FALLBACK_MODEL", "deepseek/deepseek-v4-flash")
# M15 (docs/plans/2026-10-12_rl_backfill_eval_train.md): the small open model trained with RL
# and the frontier model it is compared with, on the same tools and tasks.
RL_BASE_MODEL = os.environ.get("GRIDCASE_RL_BASE_MODEL", "Qwen/Qwen3-8B")
FRONTIER_MODEL = os.environ.get("GRIDCASE_FRONTIER_MODEL", "claude-sonnet-5-5")
BACKFILL_DIR = Path(os.environ.get("GRIDCASE_BACKFILL_DIR", DATA_DIR / "backfill"))
BACKFILL_DB = Path(os.environ.get("GRIDCASE_BACKFILL_DB", DATA_DIR / "backfill.duckdb"))
RL_DIR = Path(os.environ.get("GRIDCASE_RL_DIR", DATA_DIR / "rl"))
EVALS_DIR = ROOT / "evals"
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def api_token() -> str | None:
    """Token the `investigate` MCP tool requires; None means the tool refuses every call."""
    return os.environ.get("GRIDCASE_API_TOKEN", "").strip() or None


def auto_paid_cases_per_day() -> int:
    """Automatic investigations per UTC day that may use the paid model; the rest use the
    free standard playbook."""
    return int(os.environ.get("GRIDCASE_AUTO_PAID_PER_DAY", "10"))


def ask_daily_usd() -> float:
    """Spending cap for the question box per UTC day; questions are refused once it is spent."""
    return float(os.environ.get("GRIDCASE_ASK_DAILY_USD", "2.0"))


def has_anthropic_api_key() -> bool:
    """Presence only; the SDK reads the value itself. Never surface the key."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def has_openrouter_api_key() -> bool:
    """Presence only; the client reads the value itself. Never surface the key."""
    return bool(os.environ.get("OPENROUTER_API_KEY", "").strip())


def explore_paid_per_day() -> int:
    """Investigations per UTC day that visitors may start from the Explore page with the paid
    model (a budget separate from the automatic ones); the rest use the free standard playbook."""
    return int(os.environ.get("GRIDCASE_EXPLORE_PAID_PER_DAY", "10"))
