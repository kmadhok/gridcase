"""Paths, environment and constants. Secrets come only from .env via python-dotenv."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("GRIDCASE_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("GRIDCASE_DB", DATA_DIR / "gridcase.duckdb"))
RAW_DIR = DATA_DIR / "raw"
ONTOLOGY_DIR = ROOT / "ontology"

REGIONS = ["ERCO", "CISO", "PJM", "US48"]
HISTORY_START = datetime(2018, 7, 1)

# Models: names live in config, never in code. Prices are USD per million tokens (input, output).
PLANNER_MODEL = os.environ.get("GRIDCASE_PLANNER_MODEL", "claude-opus-5")
CHEAP_MODEL = os.environ.get("GRIDCASE_CHEAP_MODEL", "claude-sonnet-5")
ASK_MODEL = os.environ.get("GRIDCASE_ASK_MODEL", CHEAP_MODEL)
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def api_token() -> str | None:
    """Token the `investigate` MCP tool requires; None means the tool refuses every call."""
    return os.environ.get("GRIDCASE_API_TOKEN", "").strip() or None


def ask_daily_usd() -> float:
    """Spending cap for the question box per UTC day; questions are refused once it is spent."""
    return float(os.environ.get("GRIDCASE_ASK_DAILY_USD", "5.0"))


def has_anthropic_api_key() -> bool:
    """Presence only; the SDK reads the value itself. Never surface the key."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def eia_api_key() -> str:
    key = os.environ.get("EIA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("EIA_API_KEY is not set; put it in .env")
    return key
