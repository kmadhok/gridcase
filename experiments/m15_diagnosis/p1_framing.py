"""M15 diagnosis P1: single-turn, no-tool prompt A/B on the validation spike tasks.

    uv run python experiments/m15_diagnosis/p1_framing.py [--arms A,B,...] [--samples 4]

Each arm asks the untuned Qwen3-8B (thinking off, the eval temperature) for a probability with
no tools, so only the framing differs. Arms (docs/M15_DIAGNOSIS.md, plan P1):
  A  the current wording                      B   down-moves restated as rises of the other outcome
  B' up-moves restated as drops of the other  C   a `reasoning` field written before `probability`
  D  the market-implied hold probability given E   no "prices are probabilities of the first
                                                    outcome" sentence
  F  unsigned wording ("fell by 22.0 points")   BD  B and D together
Writes data/rl/diagnosis/p1/<arm>.jsonl (one row per task, all samples) and summary.json.
Spend goes through the M15 ledger under small_eval.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from gridcase import config
from gridcase.rl.cli import _load
from gridcase.rl.harness import market_baseline
from gridcase.rl.models import TINKER_PRICES
from gridcase.rl.spend import Spend
from gridcase.rl.tasks import Task

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "rl" / "diagnosis" / "p1"
ARMS = ["A", "B", "Bp", "C", "D", "E", "F", "BD"]
PRICES = "Prices are probabilities of the market's first outcome unless the question names another."


@dataclass
class Shown:
    """The move as the prompt presents it."""

    before: float
    now: float
    outcome: str  # "first" or "second"

    @property
    def move_pts(self) -> float:
        return round((self.now - self.before) * 100, 1)


def shown(task: Task, arm: str) -> Shown:
    f = task.facts
    b, n = float(f["price_before"]), float(f["price_now"])
    up = float(f["move_pts"]) > 0
    flip = (arm in ("B", "BD") and not up) or (arm == "Bp" and up)
    if flip:
        return Shown(round(1 - b, 4), round(1 - n, 4), "second")
    return Shown(b, n, "first")


def system(arm: str) -> str:
    fmt = (
        '{"reasoning": "<at most 120 words>", "probability": <number from 0 to 1>}'
        if arm == "C"
        else '{"probability": <number from 0 to 1>, "summary": "<one sentence, no numbers>"}'
    )
    prices = "" if arm == "E" else PRICES + " "
    return (
        "You are an analyst forecasting a Polymarket prediction market at a fixed moment in the "
        f"past, called as_of. There are no tools in this exercise. {prices}"
        f"Reply with only a JSON object: {fmt}"
    )


def user(task: Task, arm: str) -> str:
    f = task.facts
    s = shown(task, arm)
    when = task.as_of.strftime("%Y-%m-%d %H:%M UTC")
    later = (task.as_of + timedelta(hours=int(f["horizon_hours"]))).strftime("%Y-%m-%d %H:%M UTC")
    whose = (
        "the market's price" if s.outcome == "first" else "the price of the market's second outcome"
    )
    if arm == "F":
        verb = "rose" if s.move_pts > 0 else "fell"
        move = f"{verb} by {abs(s.move_pts)} points, from {s.before} to {s.now}"
    else:
        move = f"moved from {s.before} to {s.now} ({s.move_pts:+} points)"
    text = (
        f"Market condition_id: {task.condition_id}\nas_of: {when}\n\n"
        f"In the last {f['window_hours']} hours {whose} {move}. Question: what is the probability "
        f"that at least half of this move is still there {f['horizon_hours']} hours later, at "
        f"{later}?"
    )
    if arm in ("D", "BD"):
        q = market_baseline(task, {})
        text += (
            f" For reference, the probability implied by the current price that the price ends on "
            f"the side this move points to is {q:.3f}."
        )
    return text + " Answer now."


def parse(text: str) -> float | None:
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            p = json.loads(m.group(0)).get("probability")
            if isinstance(p, int | float) and not isinstance(p, bool) and 0 <= p <= 1:
                return float(p)
        except (json.JSONDecodeError, AttributeError):
            pass
    m = re.search(r'"probability"\s*:\s*([0-9.]+)', text)
    if m:
        try:
            p = float(m.group(1))
            return p if 0 <= p <= 1 else None
        except ValueError:
            return None
    return None


class Sampler:
    def __init__(self, samples: int, temperature: float) -> None:
        import tinker
        from tinker_cookbook.renderers import get_renderer
        from tinker_cookbook.tokenizer_utils import get_tokenizer

        self.base = config.RL_BASE_MODEL
        self.renderer = get_renderer(config.RL_RENDERER, get_tokenizer(self.base))
        self.client = tinker.ServiceClient().create_sampling_client(base_model=self.base)
        self.samples, self.temperature = samples, temperature
        self.spend = Spend(config.RL_DIR / "spend.jsonl")
        self.prefill, self.sample_price, _ = TINKER_PRICES[self.base]
        self.lock = threading.Lock()  # the fast tokenizer is not thread-safe

    def __call__(self, sys_text: str, user_text: str, max_tokens: int) -> tuple[list[str], float]:
        from tinker import types

        msgs: list[Any] = [
            {"role": "system", "content": sys_text},
            {"role": "user", "content": user_text},
        ]
        with self.lock:
            prompt = self.renderer.build_generation_prompt(msgs)
            stop = self.renderer.get_stop_sequences()
        est = (prompt.length * self.prefill + self.samples * max_tokens * self.sample_price) / 1e6
        params = types.SamplingParams(
            max_tokens=max_tokens, temperature=self.temperature,
            stop=stop,
        )  # fmt: skip
        held = self.spend.reserve("small_eval", est)
        try:
            res = self.client.sample(
                prompt=prompt, num_samples=self.samples, sampling_params=params
            ).result()
        except BaseException:
            self.spend.release(held)
            raise
        texts, n_out = [], 0
        for seq in res.sequences:
            with self.lock:
                msg, _ = self.renderer.parse_response(list(seq.tokens))
            c = msg.get("content")
            texts.append(c if isinstance(c, str) else "".join(str(x.get("text", "")) for x in c))
            n_out += len(seq.tokens)
        usd = (prompt.length * self.prefill + n_out * self.sample_price) / 1e6
        self.spend.record(
            "small_eval", f"p1:{self.base}", prompt.length, n_out, usd, reservation=held
        )
        return texts, usd


def _corr(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return cov / math.sqrt(vx * vy) if vx > 1e-12 and vy > 1e-12 else float("nan")


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-sample stats (every sample is a forecast), split by the task's true direction."""
    flat = [(r, p) for r in rows for p in r["ps"] if p is not None]
    n_all = sum(len(r["ps"]) for r in rows)
    out: dict[str, Any] = {
        "n_samples": n_all,
        "malformed": 1 - len(flat) / n_all if n_all else None,
        "brier": sum((p - r["y"]) ** 2 for r, p in flat) / len(flat),
        "market_brier": sum((r["q"] - r["y"]) ** 2 for r in rows) / len(rows),
        "dir_corr": _corr([p for _, p in flat], [float(r["direction"] == "down") for r, _ in flat]),
        "corr_p_price_now": _corr([p for _, p in flat], [r["price_now"] for r, _ in flat]),
        "corr_p_q": _corr([p for _, p in flat], [r["q"] for r, _ in flat]),
        "within_task_sd": _mean(
            [
                _sd([p for p in r["ps"] if p is not None])
                for r in rows
                if sum(p is not None for p in r["ps"]) > 1
            ]
        ),  # fmt: skip
    }
    for d in ("up", "down"):
        sel = [(r, p) for r, p in flat if r["direction"] == d]
        out[d] = {
            "n": len(sel),
            "mean_p": _mean([p for _, p in sel]),
            "mean_q": _mean([r["q"] for r, _ in sel]),
            "hold_rate": _mean([r["y"] for r, _ in sel]),
            "brier": _mean([(p - r["y"]) ** 2 for r, p in sel]),
            "corr_p_y": _corr([p for _, p in sel], [r["y"] for r, _ in sel]),
            "corr_p_q": _corr([p for _, p in sel], [r["q"] for r, _ in sel]),
            "corr_p_headroom": _corr([p for _, p in sel], [r["headroom"] for r, _ in sel]),
        }
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _sd(xs: list[float]) -> float:
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def fit_shown(rows_by_arm: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """OLS p ~ shown price_now + shown-down over arms A, B, B': does the down offset follow the
    sign the prompt shows (a misreading of drops) or the task (anchoring alone)?"""
    xs, ys = [], []
    for arm in ("A", "B", "Bp"):
        for r in rows_by_arm.get(arm, []):
            for p in r["ps"]:
                if p is not None:
                    xs.append((1.0, r["shown_now"], float(r["shown_down"])))
                    ys.append(p)
    if len(xs) < 10:
        return {}
    # normal equations for 3 parameters
    xtx = [[sum(x[i] * x[j] for x in xs) for j in range(3)] for i in range(3)]
    xty = [sum(x[i] * y for x, y in zip(xs, ys, strict=True)) for i in range(3)]
    beta = _solve(xtx, xty)
    pred = [sum(b * xi for b, xi in zip(beta, x, strict=True)) for x in xs]
    my = _mean(ys)
    r2 = 1 - sum((y - q) ** 2 for y, q in zip(ys, pred, strict=True)) / sum(
        (y - my) ** 2 for y in ys
    )
    return {"intercept": beta[0], "shown_price_now": beta[1], "shown_down": beta[2], "r2": r2,
            "n": len(ys)}  # fmt: skip


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--samples", type=int, default=4)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    tasks, answers, _ = _load("validation")
    tasks = [t for t in tasks if t.kind == "spike_hold"][: a.limit or None]
    sampler = Sampler(a.samples, a.temperature)
    OUT.mkdir(parents=True, exist_ok=True)
    rows_by_arm: dict[str, list[dict[str, Any]]] = {}
    total = 0.0
    for arm in a.arms.split(","):
        max_tokens = 400 if arm == "C" else 160

        def one(t: Task, arm: str = arm, max_tokens: int = max_tokens) -> dict[str, Any]:
            texts, usd = sampler(system(arm), user(t, arm), max_tokens)
            s = shown(t, arm)
            up = float(t.facts["move_pts"]) > 0
            pn = float(t.facts["price_now"])
            return {
                "task_id": t.task_id, "arm": arm, "direction": "up" if up else "down",
                "price_now": pn, "headroom": 1 - pn if up else pn,
                "q": market_baseline(t, {}), "y": answers[t.task_id].y,
                "shown_now": s.now, "shown_down": s.move_pts < 0, "outcome": s.outcome,
                "ps": [parse(x) for x in texts], "texts": texts, "usd": usd,
                "prompt": user(t, arm),
            }  # fmt: skip

        with ThreadPoolExecutor(a.workers) as pool:
            rows = list(pool.map(one, tasks))
        rows_by_arm[arm] = rows
        cost = sum(r["usd"] for r in rows)
        total += cost
        (OUT / f"{arm}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        st = stats(rows)
        print(
            f"{arm:3s} brier={st['brier']:.3f} market={st['market_brier']:.3f} "
            f"dir_corr={st['dir_corr']:+.2f} up/down p={st['up']['mean_p']:.2f}/"
            f"{st['down']['mean_p']:.2f} malformed={st['malformed']:.2f} ${cost:.3f}",
            flush=True,
        )
    summary = {
        "args": vars(a),
        "usd": total,
        "arms": {arm: stats(rows) for arm, rows in rows_by_arm.items()},
        "shown_sign_fit": fit_shown(rows_by_arm),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary["shown_sign_fit"], indent=1), f"total ${total:.3f}")


if __name__ == "__main__":
    main()
