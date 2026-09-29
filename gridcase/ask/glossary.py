"""Plain-language names and definitions the question box may use (mirrors web/src/labels.ts)."""

# ruff: noqa: E501  (one definition per line reads better than wrapped prose)

from __future__ import annotations

CAUSES: dict[str, tuple[str, str]] = {
    "H0_DATA_ARTIFACT": ("bad data", "The numbers themselves are wrong, missing or later revised."),
    "H1_WEATHER_DEMAND": (
        "weather-driven demand",
        "Temperature moved demand more than the forecast assumed.",
    ),
    "H2_SUPPLY_SHORTFALL": ("supply shortfall", "Power plants delivered far less than planned."),
    "H3_IMPORT_CONSTRAINT": (
        "imports maxed out",
        "The lines to neighbouring grids were at their limit.",
    ),
    "H4_LOAD_SHED": ("load shed", "Customers were deliberately cut off to protect the grid."),
    "H5_SUBREGION_CONCENTRATION": (
        "one area drove it",
        "The miss came from a single zone, not the whole grid.",
    ),
    "H6_RENEWABLE_FORECAST_MISS": (
        "wind or solar came in far off",
        "Renewable output was far from what was forecast.",
    ),
    "H7_CALENDAR": ("holiday effect", "The forecast treated a holiday like a normal day."),
}

STATUS = {
    "supported": "evidence supports",
    "confirmed": "confirmed on follow-up",
    "refuted": "ruled out",
    "superseded": "overtaken on follow-up",
    "open": "couldn't test yet",
}

REGIONS = {
    "ERCO": "ERCOT (Texas)",
    "CISO": "CAISO (California)",
    "PJM": "PJM (Mid-Atlantic)",
    "US48": "the US Lower 48",
}

CHECKS = {
    "identity.balance": "Do the numbers add up? Checks demand = generation - net exports hour by hour.",
    "revision.diff": "Was the data revised later? Compares two published versions of a series.",
    "regress.temperature": "How much does weather explain? Relates demand and the miss to temperature.",
    "peer.compare": "Did neighbouring grids see it too? Correlates the miss with other regions'.",
    "bridge.fuel": "Which power sources moved? Breaks generation down by fuel.",
    "price.co_movement": "Did prices spike? Wholesale prices near the cap.",
    "variance.plan_vs_actual": "How big was the miss? Demand vs. the day-ahead forecast.",
    "contribution.subregion": "Was one area driving it? Shares of the change by zone.",
    "baseline.expected": "What was normal for this hour? The model of a typical hour.",
    "anomaly.detect": "Was a quantity abnormal? Compares it with its normal range hour by hour.",
}

GLOSSARY = {
    "evidence supports": "The check that could have ruled this cause out did not; the numbers are consistent with it.",
    "ruled out": "A check found numbers that contradict this cause.",
    "couldn't test yet": "The data needed was not published yet at the investigation's point in time.",
    "likely": "How strongly the evidence and the region's history favour this cause, as a percentage.",
    "explains X% of the gap": "How much of the shortfall between demand and forecast this cause could account for.",
    "measured": "A sentence whose number is read straight from the data, with the exact query attached.",
    "computed": "A sentence whose number is calculated from measured numbers.",
    "interpretation": "A sentence that is the writer's judgement; no evidence link.",
    "follow-up check": "Some grid data arrives a day or two late; each case is re-checked once it lands.",
    "usual gap": "The typical difference between demand and its forecast for this region over the past 28 days.",
    "day-ahead forecast": "The grid operator's forecast of each hour's demand, published the day before.",
    "served load": "During deliberate blackouts, reported demand is the power actually delivered, not what customers wanted.",
    "point in time": "Investigations only use data that had been published by then, so they can be replayed honestly.",
}
