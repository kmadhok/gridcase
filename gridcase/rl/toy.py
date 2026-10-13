"""An offline toy of the M15 trainer, to test training-signal changes without spending.

The world: spike_hold tasks with a direction (up/down), a move-size bucket and a headroom
bucket (how far `price_now` is from the boundary the move points to). The chance a move holds
combines the committed train base rates by direction x size (evals/m15/base_rates.json) and the
post-mortem's hold rate by headroom (0.95 for headroom <= 0.05 falling to 0.44 above 0.5) on the
logit scale. Nothing in the toy sees any real task; the policy is told its context only.

The policy: a softmax over a grid of probabilities (0.05 ... 0.95) with logits
b[k] + sum_f theta[f][k] x_f over three features (down-move, headroom, move size), so a shared
bias (the "modal answer" every task pushes on) and feature terms that must be learned from
outcomes pooled across tasks, as a language model would; plus a "call another tool" logit that
sets the episode's length (10 calls at most; more than 8 costs the harness's penalty). The grid
distribution starts at the untuned Qwen3-8B's answers on eval spike tasks, by direction (mode
0.65 after an up-move, 0.35 after a down-move). Tokens: 40 per tool turn, 60 for the answer
turn; one decision token in each, the rest are filler that carries advantage but no gradient.

Training mirrors gridcase/rl/train.py: groups of samples per task that share the task's
outcome, group-relative advantages, per-token weights, Tinker's token-sum loss
(gradient = sum over tokens of advantage x grad log pi), the Cookbook's KL-penalty form, and
Adam with Tinker's defaults (beta1 0.9, beta2 0.95, eps 1e-12: tinker/types/optimizer.py).
Degenerate groups (all rewards equal) give zero advantage, as in the trainer. The toy returns
exact (not sampled) Brier scores of the final policy, against the best constant on the grid
and the best grid answer per context (the direction/headroom rule).
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from gridcase import config

P_GRID = [round(0.05 * k, 2) for k in range(1, 20)]
DIRECTIONS = ("up", "down")
SIZES = ("10-15", "15-20", "20-30", "30-101")
HEADROOM = ("<=0.05", "0.05-0.1", "0.1-0.2", "0.2-0.3", "0.3-0.5", ">0.5")
HEADROOM_HOLD = (0.95, 0.86, 0.79, 0.64, 0.57, 0.44)  # RESULTS_M15.md, post-mortem
# share of spike tasks by direction x size (large moves are rare)
SIZE_SHARE = {
    "up:10-15": 0.30, "up:15-20": 0.12, "up:20-30": 0.10, "up:30-101": 0.03,
    "down:10-15": 0.22, "down:15-20": 0.12, "down:20-30": 0.08, "down:30-101": 0.03,
}  # fmt: skip
# untuned Qwen3-8B on the eval 600's spike tasks (evals/m15/results/base_eval.json), p to the
# nearest 0.05, counts by direction
UNTUNED = {
    "up": {0.35: 1, 0.45: 8, 0.5: 5, 0.55: 8, 0.6: 4, 0.65: 93, 0.7: 9, 0.75: 41, 0.8: 2,
           0.85: 6, 0.9: 2},
    "down": {0.05: 8, 0.1: 20, 0.15: 16, 0.2: 6, 0.25: 26, 0.3: 32, 0.35: 41, 0.4: 10,
             0.45: 27, 0.5: 8, 0.55: 5, 0.6: 3, 0.65: 9},
}  # fmt: skip
TOOL_TOKENS, ANSWER_TOKENS, MAX_CALLS, BUDGET = 40, 60, 10, 8
PENALTY = 0.25
BASE_RATES_PATH = config.EVALS_DIR / "m15" / "base_rates.json"


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _softmax(z: list[float]) -> list[float]:
    m = max(z)
    e = [math.exp(v - m) for v in z]
    s = sum(e)
    return [v / s for v in e]


@dataclass(frozen=True)
class Context:
    direction: str
    size: str
    headroom: int  # index into HEADROOM
    weight: float
    p_hold: float

    @property
    def stratum(self) -> str:
        return f"{self.direction}:{self.size}"


def world(base_rates_path: Path = BASE_RATES_PATH) -> list[Context]:
    rates = json.loads(base_rates_path.read_text())
    overall = _logit(rates["spike_hold"])
    out = []
    for d in DIRECTIONS:
        for s in SIZES:
            for h in range(len(HEADROOM)):
                p = _sigmoid(_logit(rates[f"{d}:{s}"]) + _logit(HEADROOM_HOLD[h]) - overall)
                out.append(Context(d, s, h, SIZE_SHARE[f"{d}:{s}"] / len(HEADROOM), p))
    return out


@dataclass(frozen=True)
class Scheme:
    name: str
    advantage_norm: str = "mean"  # mean | group_std | batch_std
    length_norm: bool = False
    answer_weight: float = 1.0
    kl_coef: float = 0.0
    kl_target: str = "base"  # base: KL to the starting policy; uniform: an entropy bonus
    stratify: bool = False
    group_size: int = 8
    tasks_per_step: int = 16
    lr: float = 0.1  # reproduces the first run's timeline: plateau by step ~30
    adv_eps: float = 1e-3


OLD = Scheme("old")
# what the toy picked (experiments/rl_toy/results.md): batch-normalised advantages, length
# normalisation with the answer turn x4, an entropy bonus of 0.1 (KL to the base model hurt:
# the base's down-move answers are wrong, so pulling toward them is wrong), stratified batches
NEW = Scheme("new", advantage_norm="batch_std", length_norm=True, answer_weight=4.0,
             kl_coef=0.1, kl_target="uniform", stratify=True)  # fmt: skip


@dataclass
class ToyResult:
    scheme: str
    brier: float
    constant_brier: float
    rule_brier: float
    direction_corr: float
    headroom_corr: float
    spike_p_distinct: int
    degenerate_frac_last: float
    mean_calls: float
    penalised_frac: float
    mean_p: dict[str, float] = field(default_factory=dict)  # by direction and by headroom
    curve: list[dict[str, float]] = field(default_factory=list)


class _Adam:
    """Adam with Tinker's defaults (tinker/types/optimizer.py: beta1 0.9, beta2 0.95, eps 1e-12)."""

    def __init__(self, n: int, lr: float, b1: float = 0.9, b2: float = 0.95, eps: float = 1e-12):
        self.lr, self.b1, self.b2, self.eps = lr, b1, b2, eps
        self.m = [0.0] * n
        self.v = [0.0] * n
        self.t = 0

    def step(self, theta: list[float], grad: list[float]) -> None:
        self.t += 1
        c1, c2 = 1 - self.b1**self.t, 1 - self.b2**self.t
        for i, g in enumerate(grad):
            self.m[i] = self.b1 * self.m[i] + (1 - self.b1) * g
            self.v[i] = self.b2 * self.v[i] + (1 - self.b2) * g * g
            theta[i] += self.lr * (self.m[i] / c1) / (math.sqrt(self.v[i] / c2) + self.eps)


FEATURES = ("down", "headroom", "size")


def features(c: Context) -> list[float]:
    return [
        1.0 if c.direction == "down" else 0.0,
        c.headroom / (len(HEADROOM) - 1) - 0.5,
        SIZES.index(c.size) / (len(SIZES) - 1) - 0.5,
    ]


class Policy:
    def __init__(self, contexts: list[Context]) -> None:
        self.contexts = contexts
        self.x = [features(c) for c in contexts]
        k = len(P_GRID)
        # up-move contexts (down = 0) start at the untuned model's up-move histogram; the down
        # term carries the difference to its down-move histogram
        up = [UNTUNED["up"].get(p, 0) + 0.5 for p in P_GRID]
        self.b = [math.log(c / sum(up)) for c in up]
        hist = [UNTUNED["down"].get(p, 0) + 0.5 for p in P_GRID]
        down = [math.log(h / sum(hist)) for h in hist]
        self.theta = [[down[i] - self.b[i] for i in range(k)], [0.0] * k, [0.0] * k]
        self.u = [_logit(0.85)]  # ~5 tool calls, 23% over budget: the first run's start
        self.b0 = list(self.b)
        self.theta0 = [list(t) for t in self.theta]
        self.u0 = list(self.u)

    def _probs(self, ci: int, b: list[float], theta: list[list[float]]) -> list[float]:
        x = self.x[ci]
        return _softmax(
            [b[i] + sum(theta[f][i] * x[f] for f in range(len(FEATURES))) for i in range(len(b))]
        )

    def probs(self, ci: int) -> list[float]:
        return self._probs(ci, self.b, self.theta)

    def base_probs(self, ci: int) -> list[float]:
        return self._probs(ci, self.b0, self.theta0)


@dataclass
class _Episode:
    ci: int
    k: int  # grid index answered
    calls: int
    continues: list[int]  # 1 per continue decision, then 0 if it stopped before MAX_CALLS
    reward: float = 0.0
    advantage: float = 0.0


def _rollout(pol: Policy, ci: int, y: float, rng: random.Random) -> _Episode:
    cont = _sigmoid(pol.u[0])
    continues: list[int] = []
    while len([c for c in continues if c]) < MAX_CALLS:
        c = 1 if rng.random() < cont else 0
        continues.append(c)
        if not c:
            break
    calls = sum(continues)
    pi = pol.probs(ci)
    k = rng.choices(range(len(P_GRID)), weights=pi)[0]
    p = min(0.99, max(0.01, P_GRID[k]))
    r = 1 - (p - y) ** 2 - (PENALTY if calls > BUDGET else 0.0)
    return _Episode(ci, k, calls, continues, reward=r)


def _advantages(groups: list[list[_Episode]], scheme: Scheme) -> None:
    from gridcase.rl.train import batch_advantages

    advs = batch_advantages(
        [[e.reward for e in g] for g in groups], scheme.advantage_norm, scheme.adv_eps
    )
    for g, a in zip(groups, advs, strict=True):
        for e, ai in zip(g, a, strict=True):
            e.advantage = ai


def _token_weights(e: _Episode, scheme: Scheme) -> tuple[float, float]:
    """(per-token weight on tool turns, per-token weight on the answer turn)."""
    wa = scheme.answer_weight
    if not scheme.length_norm:
        return 1.0, wa
    z = TOOL_TOKENS * e.calls + ANSWER_TOKENS * wa
    return 1.0 / z, wa / z


def _train_step(
    pol: Policy, groups: list[list[_Episode]], scheme: Scheme, opt: dict[str, _Adam]
) -> None:
    k = len(P_GRID)
    nf = len(FEATURES)
    gb = [0.0] * k
    gt = [[0.0] * k for _ in range(nf)]
    gu = [0.0]
    # zero-advantage episodes (degenerate groups) still get the entropy/KL term, as in the
    # trainer (gridcase/rl/train.py::step_datums)
    eps = [e for g in groups for e in g if e.advantage != 0.0 or scheme.kl_coef]
    uniform = scheme.kl_target == "uniform"

    def ref_probs(ci: int) -> list[float]:
        return [1.0 / k] * k if uniform else pol.base_probs(ci)

    def ref_cont() -> float:
        return 0.5 if uniform else _sigmoid(pol.u0[0])

    # per-token diffs (log pi - log pi_ref) on decision tokens, zero on filler
    kl_avg = 0.0
    if scheme.kl_coef:
        num = den = 0.0
        for e in eps:
            wt, wa = _token_weights(e, scheme)
            pi, pi0 = pol.probs(e.ci), ref_probs(e.ci)
            num += wa * (math.log(pi[e.k]) - math.log(pi0[e.k]))
            c, c0 = _sigmoid(pol.u[0]), ref_cont()
            for cj in e.continues:
                w = wt if cj else wa
                num += w * (math.log(c if cj else 1 - c) - math.log(c0 if cj else 1 - c0))
            den += wt * TOOL_TOKENS * e.calls + wa * ANSWER_TOKENS
        kl_avg = num / den if den else 0.0
    for e in eps:
        wt, wa = _token_weights(e, scheme)
        pi = pol.probs(e.ci)
        a_ans = e.advantage * wa
        if scheme.kl_coef:
            pi0 = ref_probs(e.ci)
            a_ans += scheme.kl_coef * wa * (kl_avg - (math.log(pi[e.k]) - math.log(pi0[e.k])))
        x = pol.x[e.ci]
        for i in range(k):
            g = a_ans * ((1.0 if i == e.k else 0.0) - pi[i])
            gb[i] += g
            for f in range(nf):
                gt[f][i] += g * x[f]
        c = _sigmoid(pol.u[0])
        for cj in e.continues:
            a = e.advantage * (wt if cj else wa)
            if scheme.kl_coef:
                c0 = ref_cont()
                diff = math.log(c if cj else 1 - c) - math.log(c0 if cj else 1 - c0)
                a += scheme.kl_coef * (wt if cj else wa) * (kl_avg - diff)
            gu[0] += a * (cj - c)
    # the loss is -sum(adv * logprob): ascend
    opt["b"].step(pol.b, gb)
    for f in range(nf):
        opt[f"t{f}"].step(pol.theta[f], gt[f])
    opt["u"].step(pol.u, gu)


def _sample_tasks(contexts: list[Context], scheme: Scheme, rng: random.Random) -> list[int]:
    idx = list(range(len(contexts)))
    weights = [c.weight for c in contexts]
    picked: list[int] = []
    if scheme.stratify:
        strata = sorted({c.stratum for c in contexts})
        rng.shuffle(strata)
        for s in strata[: scheme.tasks_per_step]:
            members = [i for i in idx if contexts[i].stratum == s]
            picked.append(rng.choices(members, weights=[contexts[i].weight for i in members])[0])
    while len(picked) < scheme.tasks_per_step:
        picked.append(rng.choices(idx, weights=weights)[0])
    return picked


def _wcorr(xs: list[float], ys: list[float], ws: list[float]) -> float:
    sw = sum(ws)
    mx = sum(w * x for w, x in zip(ws, xs, strict=True)) / sw
    my = sum(w * y for w, y in zip(ws, ys, strict=True)) / sw
    cov = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys, strict=True))
    vx = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs, strict=True))
    vy = sum(w * (y - my) ** 2 for w, y in zip(ws, ys, strict=True))
    return cov / math.sqrt(vx * vy) if vx > 1e-12 and vy > 1e-12 else 0.0


def exact_brier(pol: Policy) -> float:
    return sum(
        c.weight * sum(pi * (c.p_hold * (1 - p) ** 2 + (1 - c.p_hold) * p**2)
                       for pi, p in zip(pol.probs(ci), P_GRID, strict=True))
        for ci, c in enumerate(pol.contexts)
    )  # fmt: skip


def reference_briers(contexts: list[Context]) -> tuple[float, float]:
    """(best constant on the grid, best grid answer per context: the direction/headroom rule)."""

    def cost(c: Context, p: float) -> float:
        return c.p_hold * (1 - p) ** 2 + (1 - c.p_hold) * p**2

    const = min(sum(c.weight * cost(c, p) for c in contexts) for p in P_GRID)
    rule = sum(c.weight * min(cost(c, p) for p in P_GRID) for c in contexts)
    return const, rule


def simulate(scheme: Scheme, steps: int = 80, seed: int = 0) -> ToyResult:
    rng = random.Random(seed)
    contexts = world()
    pol = Policy(contexts)
    k = len(P_GRID)
    opt = {"b": _Adam(k, scheme.lr), "u": _Adam(1, scheme.lr)}
    for f in range(len(FEATURES)):
        opt[f"t{f}"] = _Adam(k, scheme.lr)
    curve: list[dict[str, float]] = []
    for step in range(steps):
        tasks = _sample_tasks(contexts, scheme, rng)
        groups: list[list[_Episode]] = []
        for ci in tasks:
            y = 1.0 if rng.random() < contexts[ci].p_hold else 0.0
            groups.append([_rollout(pol, ci, y, rng) for _ in range(scheme.group_size)])
        _advantages(groups, scheme)
        flat = [e for g in groups for e in g]
        degenerate = sum(all(e.advantage == 0.0 for e in g) for g in groups) / len(groups)
        curve.append({
            "step": step, "mean_reward": sum(e.reward for e in flat) / len(flat),
            "degenerate_frac": degenerate, "brier": exact_brier(pol),
            "mean_calls": sum(e.calls for e in flat) / len(flat),
            "penalised_frac": sum(e.calls > BUDGET for e in flat) / len(flat),
            "distinct_p": float(len({e.k for e in flat})),
        })  # fmt: skip
        _train_step(pol, groups, scheme, opt)
    const, rule = reference_briers(contexts)
    mean_p = [sum(pi * p for pi, p in zip(pol.probs(ci), P_GRID, strict=True))
              for ci in range(len(contexts))]  # fmt: skip
    ws = [c.weight for c in contexts]
    mass = [sum(c.weight * pol.probs(ci)[i] for ci, c in enumerate(contexts)) for i in range(k)]
    by: dict[str, float] = {}
    for d in DIRECTIONS:
        sel = [ci for ci, c in enumerate(contexts) if c.direction == d]
        by[d] = sum(ws[ci] * mean_p[ci] for ci in sel) / sum(ws[ci] for ci in sel)
    for h, label in enumerate(HEADROOM):
        sel = [ci for ci, c in enumerate(contexts) if c.headroom == h]
        by[f"headroom {label}"] = sum(ws[ci] * mean_p[ci] for ci in sel) / sum(ws[ci] for ci in sel)
    last = curve[-5:]
    return ToyResult(
        scheme=scheme.name,
        brier=exact_brier(pol),
        constant_brier=const,
        rule_brier=rule,
        direction_corr=_wcorr(
            mean_p, [1.0 if c.direction == "down" else 0.0 for c in contexts], ws
        ),
        headroom_corr=_wcorr(mean_p, [float(c.headroom) for c in contexts], ws),
        spike_p_distinct=sum(m >= 0.02 for m in mass),
        degenerate_frac_last=sum(c["degenerate_frac"] for c in last) / len(last),
        mean_calls=last[-1]["mean_calls"],
        penalised_frac=last[-1]["penalised_frac"],
        mean_p=by,
        curve=curve,
    )
