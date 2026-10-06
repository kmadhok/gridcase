# ruff: noqa: E501  (score tables read better unwrapped)
"""M13 phase C: the whole chat, end to end, on the three best pickers.

Each model answers 10 questions through `answer_explore` (pick -> kpi.series -> write ->
verifier) on the 9-day synthetic store from tests/polymarket_synth.py. Measures how many
sentences survive the verifier, fallbacks, refusals, time and actual cost, and saves every
answer so a person can read them.

    UV_NO_SYNC=1 uv run python experiments/model_pick/write_step.py --budget-usd 0.5
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from gridcase.ask.openrouter import OpenRouterChat
from gridcase.explore.chat import ExploreAsk, answer_explore
from gridcase.store.db import connect
from tests.polymarket_synth import AS_OF, START, add_trades, build_market_store, one_market_tripled

HERE = Path(__file__).parent
MODELS = [
    "deepseek/deepseek-v4-flash",
    "google/gemini-3.1-flash-lite",
    "deepseek/deepseek-v4.1-flash",
]
PAGE = {
    "kpi": "volume_usd",
    "filters": [{"dimension": "category", "values": ["Sports"]}],
    "window": "last_7d",
}
TOP = "Which Sports markets traded the most this week?"
QUESTIONS: list[dict[str, Any]] = [
    {"q": "What was the busiest hour on this chart?"},
    {"q": TOP},
    {"q": "Break down Sports volume by trade size over the last 3 days."},
    {"q": "How many active traders were in Politics yesterday?"},
    {"q": "Compare Sports and Politics volume over the past week."},
    {"q": "What share of all volume did Crypto have in the last 24 hours?"},
    {"q": "How much are the big bettors putting into sports over the past week?"},
    {"q": "Are more fresh wallets showing up in Sports lately?"},
    {
        "q": "and in Politics?",
        "history": [
            {
                "question": TOP,
                "queries": [
                    {
                        "kpi": "volume_usd",
                        "group_by": "market",
                        "filters": [{"dimension": "category", "values": ["Sports"]}],
                        "grain": "total",
                        "window": "last_7d",
                    }
                ],
                "answer": [],
            }
        ],
    },
    {"q": "Write me a poem about football.", "expect_refusal": True},
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=MODELS)
    ap.add_argument("--budget-usd", type=float, default=0.5)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp()) / "synth.duckdb"
    conn = connect(tmp)
    build_market_store(conn)
    # some shape on top of the even base market: one Sports match triples in the last hours, and
    # a few big trades from new wallets land in Politics yesterday
    add_trades(conn, one_market_tripled())
    yday = START + timedelta(days=8, hours=10)
    add_trades(
        conn,
        [
            {
                "ts": yday + timedelta(minutes=7 * k),
                "wallet": f"new{k}",
                "usd": 2500.0,
                "condition_id": "m5",
            }
            for k in range(5)
        ],
    )
    spent, results = 0.0, {}
    for model in args.models:
        chat = OpenRouterChat(model=model, fallback=model)  # no fallback: measure the model alone
        rows = []
        for item in QUESTIONS:
            if spent >= args.budget_usd:
                rows.append({"q": item["q"], "skipped": "budget"})
                continue
            req = ExploreAsk.model_validate(
                {"question": item["q"], "page": PAGE, "history": item.get("history", [])}
            )
            t0 = time.monotonic()
            res = answer_explore(chat, req, conn.cursor(), AS_OF)
            spent += res.usage.cost_usd
            rows.append(
                {
                    "q": item["q"],
                    "seconds": round(time.monotonic() - t0, 1),
                    "cost": round(res.usage.cost_usd, 5),
                    "kept": len(res.sentences) if not res.fallback else 0,
                    "dropped": res.dropped,
                    "fallback": res.fallback,
                    "refused": res.refused,
                    "refusal_ok": res.refused == bool(item.get("expect_refusal")),
                    "queries": [q.model_dump(mode="json", exclude_none=True) for q in res.queries],
                    "answer": [f"[{s.tier}] {s.text}" for s in res.sentences],
                }
            )
        done = [r for r in rows if "skipped" not in r]
        summary = {
            "kept": sum(r["kept"] for r in done),
            "dropped": sum(r["dropped"] for r in done),
            "fallbacks": sum(r["fallback"] for r in done),
            "refusals_right": f"{sum(r['refusal_ok'] for r in done)} of {len(done)}",
            "p50_seconds": sorted(r["seconds"] for r in done)[len(done) // 2],
            "cost_usd": round(sum(r["cost"] for r in done), 4),
        }
        results[model] = {"summary": summary, "rows": rows}
        print(model, summary, flush=True)
    (HERE / "results_write.json").write_text(
        json.dumps(
            {
                "as_of": AS_OF.isoformat(),
                "page": PAGE,
                "models": results,
                "total_cost_usd": round(spent, 4),
            },
            indent=1,
            default=str,
        )
    )
    print(f"total ${spent:.4f}")


if __name__ == "__main__":
    main()
