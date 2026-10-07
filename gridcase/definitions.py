"""What the site says about how every number is calculated: each public KPI's definition, how
each slice is defined, the rule for an hour's usual range, and where the data comes from.
Built from the same files and constants the code runs on (ontology/kpis.yaml,
ontology/watch.yaml, the collector and loader), so the explanation cannot drift from the
calculation. No queries: cheap enough to fetch beside any chart."""

from __future__ import annotations

from functools import cache
from typing import Any

from gridcase import config
from gridcase.catalog import _definition, _formula
from gridcase.ingest.polymarket import BASE_URL, COLLECT_EVERY_S, PAGE
from gridcase.ingest.polymarket_load import GAMMA_URL
from gridcase.market_alarm import load_watch
from gridcase.semantic.kpis import is_internal, load_kpis


def _minutes(seconds: float) -> str:
    m = seconds / 60
    return f"{m:g} minute{'' if m == 1 else 's'}"


@cache
def definitions() -> dict[str, Any]:
    kpis, dims = load_kpis()
    p = load_watch()[1]
    return {
        "kpis": [
            {
                "id": kid,
                "label": k.label,
                "description": k.description,
                "unit": k.unit,
                "formula": _formula(k, kpis),
                "definition": _definition(k),
            }
            for kid, k in kpis.items()
            if not is_internal(k)
        ],
        "dimensions": [
            {"id": d.id, "label": d.label, "values": d.values, "sql": d.sql.strip()}
            for d in dims.values()
        ],
        "usual_range": {
            "lookback_days": p.lookback_days,
            "min_history_days": p.min_history_days,
            "k_sigma": p.k_sigma,
            "min_run": p.min_run,
            "single_hour_sigma": p.single_hour_sigma,
            "settle_minutes": p.settle_minutes,
            "log_scale": list(p.log_scale),
            "warmup_days": dict(p.warmup_days),
        },
        "sources": [
            {
                "name": "Trades",
                "url": f"{BASE_URL}/trades",
                "how": (
                    "Polymarket's public trades feed. Our collector saves the newest "
                    f"{PAGE:,} trades every {_minutes(COLLECT_EVERY_S)} (the feed reaches back "
                    "only about that far), as hourly compressed files, and records any gap "
                    "between pages."
                ),
            },
            {
                "name": "Loading",
                "how": (
                    f"Every {_minutes(config.POLYMARKET_LOAD_EVERY_S)} the server loads new "
                    "files into its trades table, keeping each trade once (identified by its "
                    "transaction hash, outcome, side, size and price)."
                ),
            },
            {
                "name": "Markets and categories",
                "url": GAMMA_URL,
                "how": (
                    "Polymarket's public market data gives each market's event and tags; the "
                    "tags decide its category. Markets without a matching tag are placed by "
                    "keywords in their title, otherwise Other."
                ),
            },
            {
                "name": "As of",
                "how": (
                    "Every number reads only the trades our collector had saved by its time "
                    "(trades_as_of), so it never changes when later data arrives. Hours our "
                    "collector partly missed are never judged."
                ),
            },
        ],
    }
