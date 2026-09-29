"""Plain-language names and definitions the question box may use (mirrors web/src/labels.ts)."""

# ruff: noqa: E501  (one definition per line reads better than wrapped prose)

from __future__ import annotations

CAUSES: dict[str, tuple[str, str]] = {
    "D0_DATA_PROBLEM": (
        "a data problem",
        "Our collector missed trades or the feed stalled, so the numbers are not real movement.",
    ),
    "D1_ONE_MARKET": (
        "one market",
        "A single market (an event) accounts for most of the change: news, a live match, a resolution.",
    ),
    "D2_ONE_CATEGORY": (
        "one category",
        "One category (sports, politics, crypto…) accounts for most of the change.",
    ),
    "D3_NEW_TRADERS": (
        "new traders",
        "Wallets new to Polymarket drive the change: an acquisition wave or its absence.",
    ),
    "D4_WHALES": (
        "a few large trades",
        "Trades of $1k and up drive the change, not ordinary traders.",
    ),
    "D5_LONGSHOTS": ("long-shot bets", "Bets on outcomes priced under 20% drive the change."),
    "D6_BROAD_BASED": (
        "a broad shift",
        "The change is spread across many markets: a platform-wide shift.",
    ),
}

STATUS = {
    "supported": "evidence supports",
    "confirmed": "confirmed on follow-up",
    "refuted": "ruled out",
    "superseded": "overtaken on follow-up",
    "open": "couldn't test yet",
}

CHECKS = {
    "kpi.compare": "How big was the change? The KPI over these hours vs the same hours on previous days.",
    "data.coverage": "Is our data complete? Collector gaps, hours with no trades, freshness.",
    "by.market": "Which market drove it? The change broken down by market.",
    "by.category": "Which category drove it? The change broken down by category.",
    "by.size": "Big or small trades? The change broken down by trade size.",
    "by.trader": "New or returning traders? The change broken down by trader type.",
    "by.odds": "Long shots or favourites? The change broken down by the odds traded.",
    "kpi.alarm": "Why was this investigation opened? The KPI for each hour vs the same hour of the day on previous days; 2 hours in a row beyond 3 standard deviations (or 1 beyond 5) opens a case.",
}

GLOSSARY = {
    "trading volume": "Dollars traded: shares times price, summed over trades.",
    "active traders": "Distinct wallets that traded.",
    "new traders": "Wallets whose first trade in our record was that day (our record starts 29 Sep 2026).",
    "average trade size": "Trading volume divided by the number of trades.",
    "returning-trader share": "The share of active traders who had traded on an earlier day.",
    "usual level": "The average for the same hours of the day on previous days.",
    "share of the change": "How much of the difference from the usual level a segment accounts for.",
    "evidence supports": "The check that could have ruled this driver out did not.",
    "ruled out": "A check found numbers that contradict this driver.",
    "couldn't test yet": "Not enough history yet, or the data needed was missing.",
    "likely": "How strongly the evidence and past cases favour this driver, as a percentage.",
    "measured": "A sentence whose number is read straight from the data, with the exact query attached.",
    "computed": "A sentence whose number is calculated from measured numbers.",
    "interpretation": "A sentence that is the writer's judgement; no evidence link.",
    "long shot": "An outcome priced under 20 cents (under a 20% chance).",
    "whale": "A trade of $1,000 or more.",
    "point in time": "Investigations use only trades our collector had seen by then, so they can be replayed honestly.",
}
