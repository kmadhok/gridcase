"""Confidence per hypothesis: a logistic on the falsifier margin, anchored at the prior and
penalised when the mechanism can only account for a small part of the miss.

confidence = sigmoid(logit(prior_mean) + K * margin + C * log(coverage))
margin None (untested/unknown) leaves the prior unchanged; coverage is the share of the miss
the mechanism explains, clipped to [COVERAGE_FLOOR, 1]; None means unknown (no penalty).
Margins are clipped to [-2, 2] by the falsifier module.
"""

from __future__ import annotations

import math

from gridcase.planner.hypotheses import Prior

K = 1.0
C = 0.5
COVERAGE_FLOOR = 0.02


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def confidence(
    prior: Prior, margin: float | None, coverage: float | None = None, k: float = K, c: float = C
) -> float:
    p0 = prior.mean()
    if margin is None:
        return p0
    x = _logit(p0) + k * margin
    if coverage is not None:
        x += c * math.log(min(max(coverage, COVERAGE_FLOOR), 1.0))
    return _sigmoid(x)
