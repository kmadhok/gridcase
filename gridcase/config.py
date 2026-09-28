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


def eia_api_key() -> str:
    key = os.environ.get("EIA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("EIA_API_KEY is not set; put it in .env")
    return key
