"""M15 rewards and scores: Brier-based reward, log loss, calibration, and a paired bootstrap
over news clusters for comparing two models on the same tasks."""

from __future__ import annotations

import math
import random
from collections import defaultdict

P_MIN, P_MAX = 0.01, 0.99
MALFORMED = -0.25  # no valid answer: below every valid answer's reward (>= 0)
PENALTY = 0.25  # tool budget ran out, or the verifier dropped more than half the claims


def clip(p: float) -> float:
    return min(P_MAX, max(P_MIN, p))


def brier(p: float, y: float) -> float:
    return (clip(p) - y) ** 2


def log_loss(p: float, y: float) -> float:
    q = clip(p)
    return -(y * math.log(q) + (1 - y) * math.log(1 - q))


def reward_for(p: float | None, y: float, malformed: bool, penalised: bool) -> float:
    if malformed or p is None:
        return MALFORMED
    return 1.0 - brier(p, y) - (PENALTY if penalised else 0.0)


def ece(ps: list[float], ys: list[float], bins: int = 10) -> float:
    """Expected calibration error over equal-width probability bins."""
    if not ps:
        return 0.0
    groups: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for p, y in zip(ps, ys, strict=True):
        groups[min(bins - 1, int(clip(p) * bins))].append((clip(p), y))
    n = len(ps)
    return sum(
        len(g) / n * abs(sum(p for p, _ in g) / len(g) - sum(y for _, y in g) / len(g))
        for g in groups.values()
    )


def paired_bootstrap(
    a: dict[str, float],
    b: dict[str, float],
    cluster_of: dict[str, str],
    draws: int = 2000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Mean of a - b over shared tasks, with a 95% interval from resampling whole clusters
    (tasks in one news cluster are not independent)."""
    shared = sorted(set(a) & set(b))
    by_cluster: dict[str, list[float]] = defaultdict(list)
    for t in shared:
        by_cluster[cluster_of[t]].append(a[t] - b[t])
    clusters = sorted(by_cluster)
    if not clusters:
        return 0.0, 0.0, 0.0
    mean = sum(a[t] - b[t] for t in shared) / len(shared)
    rng = random.Random(seed)
    stats = []
    for _ in range(draws):
        pick = rng.choices(clusters, k=len(clusters))
        vals = [d for c in pick for d in by_cluster[c]]
        stats.append(sum(vals) / len(vals))
    stats.sort()
    return mean, stats[int(0.025 * draws)], stats[min(draws - 1, int(0.975 * draws))]
