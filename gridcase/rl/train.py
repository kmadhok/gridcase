"""M15 RL training: GRPO-style LoRA RL on Tinker over the train split.

Each step: save the current weights for sampling (on-policy), run `group_size` episodes per task
through the same agent loop and tools as the eval, score each with the reward
(gridcase/rl/reward.py), give each episode a group-relative advantage, turn every sampled turn
into a Datum (the turn's full prompt plus its sampled tokens, shifted by one; sampler logprobs
and the per-token advantage on the sampled tokens, zeros on the prompt) and apply
`importance_sampling` + Adam. Tinker's loss sums token-level terms over the sequence
(tinker-docs "Loss functions": `loss = -(exp(target_logprobs - sampling_logprobs) * advantages)
.sum()`, read 2026-10-12), so the advantage tensor is the only place to weight tokens and
episodes.

Step 2 (2026-10-12, after the first run collapsed to a constant 0.65 on spikes); every item is a
TrainConfig knob:
- advantages: reward minus the group mean, then divided by the step's pooled within-group
  spread (`advantage_norm="batch_std"`), so the update does not shrink with the policy's own
  spread (sigma^2) as it sharpens. Per-group std ("group_std") is also available but biased for
  a Brier reward (see gridcase/rl/toy.py); "mean" is the first run's rule.
- length normalisation: every episode carries the same total weight whatever its token count,
  and the turn that calls submit_answer gets `answer_turn_weight` x per token.
- KL penalty to the base model on sampled tokens, in the Cookbook's form
  (tinker_cookbook/rl/metrics.py::incorporate_kl_penalty: coef * mask * (avg_diff - diff) added
  to the advantages, diff = logp_sampled - logp_base), with the per-token weights as the mask;
  reference logprobs from a base-model SamplingClient.compute_logprobs
  (tinker/lib/public_interfaces/sampling_client.py:616).
- stratified batches: every step carries one task from each direction x size bucket of
  spike_hold; the resolve share is unchanged.
- the step count is re-planned every step from the remaining allowance and the running average
  cost per step (minus a reserve, at most max_steps).
- a collapse guard: the fraction of degenerate groups (all rewards equal) and the spread of
  spike_hold answers are logged every step; training stops when collapse persists.
- one compact row per training episode goes to data/rl/train_rollouts.jsonl.

Every eval_every steps a sampler checkpoint is scored on the validation split, never the eval
split; the best validation Brier is kept. It stops early after `patience` checkpoints without
improvement, on a reward-hacking signal (verifier drops more than doubled, or tool use
collapsing to nothing), on the collapse guard, or at the spend cap.
"""

from __future__ import annotations

import json
import math
import random
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gridcase import config
from gridcase.rl.harness import bucket


@dataclass(frozen=True)
class TrainConfig:
    group_size: int = 8
    tasks_per_step: int = 16
    spike_share: float = 2 / 3  # spike_hold : resolve = 2 : 1
    learning_rate: float = 4e-5
    rank: int = 32
    temperature: float = 1.0
    max_steps: int = 150
    pilot_steps: int = 3
    eval_every: int = 10
    patience: int = 3
    reserve_usd: float = 5.0
    workers: int = 16
    seed: int = 15
    ttl_days: int = 30
    # step 2
    advantage_norm: str = "batch_std"  # mean | group_std | batch_std
    adv_eps: float = 1e-3
    length_norm: bool = True
    answer_turn_weight: float = 4.0
    kl_coef: float = 0.1  # 0 disables the KL term
    kl_target: str = "uniform"  # uniform: an entropy bonus; base: KL to the untouched base model
    stratify: bool = True
    collapse_degenerate_frac: float = 0.5
    collapse_std_max: float = 0.05  # spike answers' spread across the step's tasks
    collapse_distinct_min: int = 3
    collapse_patience: int = 3
    final_eval: bool = True  # score the best checkpoint on the eval 600 at the end

    @property
    def episodes_per_step(self) -> int:
        return self.group_size * self.tasks_per_step


@dataclass
class DatumSpec:
    """A Tinker Datum as plain lists (built offline, converted at the API boundary).

    `weights` is the per-token weight behind `advantages` (zeros on the prompt); it is the mask
    for the KL term and is not sent to Tinker (RL losses reject extra keys)."""

    input_tokens: list[int]
    target_tokens: list[int]
    logprobs: list[float]
    advantages: list[float]
    weights: list[float] = field(default_factory=list)


def _degenerate(rewards: list[float]) -> bool:
    return not rewards or max(rewards) - min(rewards) < 1e-12


def group_advantages(rewards: list[float], norm: str = "mean", eps: float = 1e-3) -> list[float]:
    """Reward minus the group mean; `norm="group_std"` (or "std") also divides by the group's
    standard deviation plus eps. All zeros when the group has no spread."""
    if _degenerate(rewards):
        return [0.0] * len(rewards)
    m = sum(rewards) / len(rewards)
    centred = [r - m for r in rewards]
    if norm in ("std", "group_std"):
        sd = math.sqrt(sum(c * c for c in centred) / len(centred))
        return [c / (sd + eps) for c in centred]
    return centred


def pooled_spread(advantages: list[list[float]]) -> float:
    """Root mean square of the mean-centred advantages over every episode in non-degenerate
    groups: the step's pooled within-group spread."""
    vals = [a for g in advantages for a in g if any(x != 0.0 for x in g)]
    if not vals:
        return 0.0
    return math.sqrt(sum(a * a for a in vals) / len(vals))


def batch_advantages(
    rewards: list[list[float]], norm: str = "batch_std", eps: float = 1e-3
) -> list[list[float]]:
    """Advantages for every group of a step. "batch_std" centres each group and divides all of
    them by the step's pooled spread, so the non-degenerate advantages have about unit RMS
    (eps aside) however sharp the policy is, while the relative sizes across groups (which
    carry the Brier reward's direction, see gridcase/rl/toy.py) are kept. The pooled spread is
    still a sample statistic of the step: with a single non-degenerate group it equals that
    group's own spread."""
    if norm == "batch_std":
        centred = [group_advantages(g, "mean") for g in rewards]
        s = pooled_spread(centred)
        return [[a / (s + eps) for a in g] for g in centred]
    return [group_advantages(g, norm, eps) for g in rewards]


def trainable(episodes: list[Any]) -> list[Any]:
    """Episodes ended by an API error or the spend cap say nothing about the policy."""
    return [e for e in episodes if e.error is None]


def _usable(turn: Any) -> bool:
    if not turn or not turn.get("tokens"):
        return False
    return len(turn.get("logprobs") or []) == len(turn["tokens"])


def turn_weights(
    raw_turns: list[Any], answer_turn: int | None, answer_weight: float, length_norm: bool
) -> list[float]:
    """Per-token weight for each turn's sampled tokens: `answer_weight` on the turn that called
    submit_answer, 1 elsewhere, 0 on turns that cannot be trained. With `length_norm` the
    weights are divided by the episode's weighted token count, so every episode's tokens sum
    to one whatever its length."""
    w = [
        (answer_weight if i == answer_turn else 1.0) if _usable(t) else 0.0
        for i, t in enumerate(raw_turns)
    ]
    if length_norm:
        total = sum(wi * len(t["tokens"]) for wi, t in zip(w, raw_turns, strict=True) if wi)
        if total > 0:
            w = [wi / total for wi in w]
    return w


def episode_datums(
    raw_turns: list[Any], advantage: float, weights: list[float] | None = None
) -> list[DatumSpec]:
    """One Datum per usable turn; the sampled tokens carry advantage x the turn's weight."""
    out = []
    for i, t in enumerate(raw_turns):
        if not _usable(t):
            continue
        w = 1.0 if weights is None else weights[i]
        prompt, sampled = list(t["prompt_tokens"]), list(t["tokens"])
        full = prompt + sampled
        n_prompt = len(prompt) - 1
        out.append(
            DatumSpec(
                input_tokens=full[:-1],
                target_tokens=full[1:],
                logprobs=[0.0] * n_prompt + list(t["logprobs"]),
                advantages=[0.0] * n_prompt + [advantage * w] * len(sampled),
                weights=[0.0] * n_prompt + [w] * len(sampled),
            )
        )
    return out


def apply_kl(
    datums: list[DatumSpec], ref_logprobs: list[list[float | None]], coef: float
) -> dict[str, float]:
    """Add the Cookbook's KL-penalty term to the advantages in place: coef * w_t * (avg - diff_t)
    with diff_t = logp_sampled - logp_base on weighted tokens and avg their weighted mean
    (tinker_cookbook/rl/metrics.py::incorporate_kl_penalty, which uses the action mask where
    this uses the per-token weights). `ref_logprobs[i]` is aligned with datum i's targets; None
    where the base model was not asked. Returns the mean logprob gap (kl_policy_base)."""
    diffs: list[list[float]] = []
    num = den = 0.0
    for d, ref in zip(datums, ref_logprobs, strict=True):
        row = []
        for lp, w, r in zip(d.logprobs, d.weights, ref, strict=True):
            diff = (lp - r) if (w > 0 and r is not None) else 0.0
            row.append(diff)
            if w > 0 and r is not None:
                num += w * diff
                den += w
        diffs.append(row)
    avg = num / den if den > 0 else 0.0
    if coef:
        for d, row in zip(datums, diffs, strict=True):
            d.advantages = [
                a + coef * w * (avg - diff)
                for a, w, diff in zip(d.advantages, d.weights, row, strict=True)
            ]
    return {"kl_policy_base": avg}


def steps_affordable(remaining_usd: float, usd_per_step: float, reserve: float, cap: int) -> int:
    if usd_per_step <= 0:
        return cap
    return max(0, min(cap, math.floor((remaining_usd - reserve) / usd_per_step)))


def plan_steps(step: int, spent: float, remaining: float, reserve: float, cap: int) -> int:
    """The total step count the allowance affords: the steps taken plus what the remaining
    allowance (minus the reserve) buys at the running average cost per step."""
    if step <= 0 or spent <= 0:
        return cap
    return min(cap, step + steps_affordable(remaining, spent / step, reserve, cap))


def pick_checkpoint(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The validation-best checkpoint (ties: the earlier one). Never looks at eval scores."""
    scored = [h for h in history if h.get("val_brier") is not None]
    return min(scored, key=lambda h: (h["val_brier"], h["step"])) if scored else None


def should_stop(history: list[dict[str, Any]], patience: int) -> bool:
    scored = [h["val_brier"] for h in history if h.get("val_brier") is not None]
    if len(scored) <= patience:
        return False
    return bool(min(scored[-patience:]) >= min(scored[:-patience]))


def hacking_signal(first: dict[str, float], now: dict[str, float]) -> str | None:
    """Verifier drops per episode more than doubled (and above 0.5), or tool use collapsed."""
    if now["dropped"] > max(0.5, 2 * first["dropped"]):
        return (
            f"verifier drops per episode rose from {first['dropped']:.2f} to {now['dropped']:.2f}"
        )
    if first["tool_calls"] >= 1 and now["tool_calls"] < 0.2:
        return (
            f"tool calls per episode fell from {first['tool_calls']:.2f} to {now['tool_calls']:.2f}"
        )
    return None


def step_metrics(groups: list[list[dict[str, Any]]]) -> dict[str, float]:
    """Collapse metrics for one step from rollout rows (kind, p, reward) grouped by task: the
    fraction of degenerate groups (all rewards equal), overall and on spike_hold, and the
    spread and distinct count (2 dp) of spike_hold answers."""
    groups = [g for g in groups if g]
    spike = [g for g in groups if g[0]["kind"] == "spike_hold"]
    ps = [r["p"] for g in spike for r in g if r.get("p") is not None]
    mean = sum(ps) / len(ps) if ps else 0.0
    return {
        "degenerate_frac": (
            sum(_degenerate([r["reward"] for r in g]) for g in groups) / len(groups)
            if groups
            else 0.0
        ),
        "degenerate_frac_spike": (
            sum(_degenerate([r["reward"] for r in g]) for g in spike) / len(spike) if spike else 0.0
        ),
        "spike_p_std": math.sqrt(sum((p - mean) ** 2 for p in ps) / len(ps)) if ps else 0.0,
        "spike_p_distinct": float(len({round(p, 2) for p in ps})),
    }


def collapse_signal(recent: list[dict[str, float]], cfg: TrainConfig) -> str | None:
    """Collapse that persisted for `collapse_patience` steps: spike_hold answers nearly
    constant across the step's tasks (spread <= collapse_std_max) while most groups are
    degenerate, or too few distinct spike_hold answers. A deterministic policy whose answers
    still differ across tasks is not collapse (its groups are degenerate, but the entropy term
    keeps acting on them), so the degenerate fraction alone never stops training."""
    last = recent[-cfg.collapse_patience :]
    if len(last) < cfg.collapse_patience:
        return None
    if all(
        m["degenerate_frac_spike"] >= cfg.collapse_degenerate_frac
        and m["spike_p_std"] <= cfg.collapse_std_max
        for m in last
    ):
        return (
            f"spike_hold groups >= {cfg.collapse_degenerate_frac:.0%} degenerate with answer "
            f"spread <= {cfg.collapse_std_max} for {cfg.collapse_patience} steps"
        )
    if all(m["spike_p_distinct"] <= cfg.collapse_distinct_min for m in last):
        return (
            f"<= {cfg.collapse_distinct_min} distinct spike_hold answers for "
            f"{cfg.collapse_patience} steps"
        )
    return None


def make_batch(
    tasks: list[Any], n: int, spike_share: float, rng: random.Random, stratify: bool = True
) -> list[Any]:
    """`n` tasks: round(n * spike_share) spike_hold and the rest resolve. Stratified: one spike
    task from each direction x size bucket first (in random bucket order), the rest at random."""
    spikes = [t for t in tasks if t.kind == "spike_hold"]
    res = [t for t in tasks if t.kind == "resolve"]
    k = min(len(spikes), round(n * spike_share))
    picked: list[Any] = []
    if stratify:
        by_bucket: dict[str, list[Any]] = defaultdict(list)
        for t in spikes:
            mv = t.facts.get("move_pts")
            by_bucket[bucket(float(mv)) if mv is not None else "?"].append(t)
        order = sorted(by_bucket)
        rng.shuffle(order)
        for b in order[:k]:
            picked.append(rng.choice(by_bucket[b]))
    chosen = {t.task_id for t in picked}
    rest = [t for t in spikes if t.task_id not in chosen]
    picked += rng.sample(rest, k - len(picked))
    return picked + rng.sample(res, min(len(res), n - k))


def rollout_row(step: int, task: Any, ep: Any, reward: float, advantage: float) -> dict[str, Any]:
    """One compact line per training episode (no transcript, no tokens)."""
    b = bucket(float(task.facts["move_pts"])) if task.kind == "spike_hold" else "resolve"
    return {
        "step": step, "task_id": task.task_id, "kind": task.kind, "bucket": b,
        "p": None if ep.probability is None else round(ep.probability, 6),
        "reward": round(reward, 6), "turns": ep.turns, "tool_calls": ep.tool_calls,
        "malformed": ep.malformed, "penalised": ep.penalised, "advantage": round(advantage, 6),
        "tokens": sum(len(t["tokens"]) for t in ep.raw_turns if t and t.get("tokens")),
        "error": None if ep.error is None else str(ep.error)[:120],
    }  # fmt: skip


def append_rollouts(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def step_datums(
    groups: list[list[Any]], advantages: list[list[float]], cfg: TrainConfig
) -> list[DatumSpec]:
    """Training data for one step. Episodes with a zero advantage (degenerate groups) carry
    no reward signal; with the entropy/KL term on they are still trained, on that term alone,
    because degenerate groups are exactly where the spread has to be restored."""
    data: list[DatumSpec] = []
    for eps, adv in zip(groups, advantages, strict=True):
        for e, a in zip(eps, adv, strict=True):
            if a == 0.0 and not cfg.kl_coef:
                continue
            w = turn_weights(e.raw_turns, e.answer_turn, cfg.answer_turn_weight, cfg.length_norm)
            data += episode_datums(e.raw_turns, a, w)
    return data


def config_from_args(args: Any) -> TrainConfig:
    keys = {
        "tasks_per_step", "group_size", "learning_rate", "max_steps", "kl_coef",
        "answer_turn_weight", "advantage_norm", "stratify", "length_norm", "eval_every",
        "kl_target", "final_eval",
    }  # fmt: skip
    given = {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}
    return TrainConfig(**given)


def train_main(args: Any) -> int:  # pragma: no cover - needs Tinker and a key
    if not config.has_tinker_api_key():
        raise SystemExit("TINKER_API_KEY is not set (.env): training runs on Tinker")
    import tinker
    from tinker import types

    from gridcase.rl.agent import run_episode
    from gridcase.rl.cli import M15, _load, evaluate
    from gridcase.rl.harness import run_eval
    from gridcase.rl.models import TINKER_PRICES, TinkerAgent
    from gridcase.rl.reward import reward_for
    from gridcase.rl.spend import OverBudget, Spend
    from gridcase.store.db import connect

    cfg = config_from_args(args)
    rng = random.Random(cfg.seed)
    train, answers, _ = _load("train")
    val, val_answers, rates = _load("validation")
    spend = Spend(config.RL_DIR / "spend.jsonl")
    con = connect(config.BACKFILL_DB, read_only=True)
    service = tinker.ServiceClient()
    tc = service.create_lora_training_client(base_model=config.RL_BASE_MODEL, rank=cfg.rank)
    # the KL reference: the untouched base model, scored with compute_logprobs (prefill price)
    use_ref = bool(cfg.kl_coef) and cfg.kl_target == "base"
    ref = service.create_sampling_client(base_model=config.RL_BASE_MODEL) if use_ref else None
    prefill_price, _, train_price = TINKER_PRICES.get(config.RL_BASE_MODEL, (0.3, 1.0, 1.0))
    log_path = config.RL_DIR / "train_log.jsonl"
    rollouts_path = config.RL_DIR / "train_rollouts.jsonl"
    history: list[dict[str, Any]] = []
    metrics_log: list[dict[str, float]] = []
    first_stats: dict[str, float] | None = None
    max_steps = cfg.max_steps
    start_spent = spend.spent("train")
    lock = threading.Lock()
    step = 0
    stop_reason = "max steps"

    def checkpoint(step: int) -> None:
        ck = tc.save_weights_for_sampler(f"ckpt-{step}", ttl_seconds=cfg.ttl_days * 86400).result()
        vagent = TinkerAgent(model_path=ck.path, phase="small_eval")
        out = M15 / "results" / f"val_step{step}_validation.json"
        res = run_eval(f"val_step{step}", val, val_answers, rates, out, conn=con, model=vagent,
                       spend=spend, workers=cfg.workers)  # fmt: skip
        history.append({"step": step, "path": ck.path, "val_brier": res["summary"]["all"]["brier"]})
        (M15 / "results" / "train_history.json").write_text(json.dumps(history, indent=1))

    try:
        while step < max_steps:
            sampler = tc.save_weights_and_get_sampling_client(name=f"step-{step}")
            agent = TinkerAgent(sampling_client=sampler, phase="train", temperature=cfg.temperature)
            batch = make_batch(train, cfg.tasks_per_step, cfg.spike_share, rng, cfg.stratify)
            results: dict[str, list[Any]] = {t.task_id: [] for t in batch}

            def roll(
                task: Any, agent: TinkerAgent = agent, results: dict[str, list[Any]] = results
            ) -> None:
                cur = con.cursor()
                try:
                    ep = run_episode(agent, cur, task, spend)
                finally:
                    cur.close()
                with lock:
                    results[task.task_id].append(ep)

            jobs = [t for t in batch for _ in range(cfg.group_size)]
            with ThreadPoolExecutor(cfg.workers) as pool:
                list(pool.map(roll, jobs))
            groups = [trainable(results[t.task_id]) for t in batch]
            rewards = [
                [
                    reward_for(e.probability, answers[t.task_id].y, e.malformed, e.penalised)
                    for e in g
                ]
                for t, g in zip(batch, groups, strict=True)
            ]
            advs = batch_advantages(rewards, cfg.advantage_norm, cfg.adv_eps)
            data = step_datums(groups, advs, cfg)
            rows: list[dict[str, Any]] = []
            metric_groups: list[list[dict[str, Any]]] = []
            dropped = calls = 0
            for t, eps, rw, adv in zip(batch, groups, rewards, advs, strict=True):
                metric_groups.append(
                    [
                        {"kind": t.kind, "p": e.probability, "reward": r}
                        for e, r in zip(eps, rw, strict=True)
                    ]
                )
                for e, r, a in zip(eps, rw, adv, strict=True):
                    rows.append(rollout_row(step, t, e, r, a))
                    dropped += len(e.dropped)
                    calls += e.tool_calls
                for e in results[t.task_id]:
                    if e.error is not None:  # not trained on, still on the record
                        rows.append(
                            rollout_row(step, t, e, reward_for(None, 0.0, True, False), 0.0)
                        )
            append_rollouts(rollouts_path, rows)
            metrics = step_metrics(metric_groups)
            metrics_log.append(metrics)
            n_eps = max(1, len(jobs))
            stats = {"dropped": dropped / n_eps, "tool_calls": calls / n_eps}
            first_stats = first_stats or stats
            kl: dict[str, float] = {}
            if data and cfg.kl_coef and ref is None:
                # entropy bonus: a uniform reference, whose constant logprob cancels in avg - diff
                kl = apply_kl(data, [[0.0] * len(d.logprobs) for d in data], cfg.kl_coef)
            elif data and ref is not None:
                n_sampled = [sum(1 for w in d.weights if w > 0) for d in data]
                n_ref = sum(len(d.input_tokens) + 1 for d in data)
                spend.reserve("train", n_ref * prefill_price / 1e6)
                futures = [
                    ref.compute_logprobs(
                        types.ModelInput.from_ints(d.input_tokens + [d.target_tokens[-1]]),
                        prompt_logprobs_last_n=n,
                    )
                    for d, n in zip(data, n_sampled, strict=True)
                ]
                # entry j scores token j given the prefix, so targets align with [1:]
                refs = [list(f.result())[1:] for f in futures]
                kl = apply_kl(data, refs, cfg.kl_coef)
                spend.record(
                    "train", f"{config.RL_BASE_MODEL}:kl_ref", n_ref, 0,
                    n_ref * prefill_price / 1e6,
                )  # fmt: skip
            if data:
                n_tok = sum(len(d.input_tokens) for d in data)
                spend.reserve("train", n_tok * train_price / 1e6)
                # Datum.__post_init__ converts raw lists to TensorData (int64 / float32)
                datums = []
                for d in data:
                    inputs: dict[str, Any] = {
                        "target_tokens": d.target_tokens,
                        "logprobs": d.logprobs,
                        "advantages": d.advantages,
                    }
                    datums.append(
                        types.Datum(
                            model_input=types.ModelInput.from_ints(d.input_tokens),
                            loss_fn_inputs=inputs,
                        )
                    )
                tc.forward_backward(datums, "importance_sampling").result()
                tc.optim_step(types.AdamParams(learning_rate=cfg.learning_rate)).result()
                spend.record(
                    "train", f"{config.RL_BASE_MODEL}:train", n_tok, 0, n_tok * train_price / 1e6
                )
            step += 1
            if step >= cfg.pilot_steps:
                max_steps = plan_steps(
                    step, spend.spent("train") - start_spent, spend.remaining("train"),
                    cfg.reserve_usd, cfg.max_steps,
                )  # fmt: skip
            flat = [r for g in rewards for r in g]
            row = {
                "step": step - 1,
                "mean_reward": sum(flat) / len(flat) if flat else None,
                "datums": len(data),
                "episodes": len(flat),
                **stats,
                **metrics,
                **kl,
                "planned_steps": max_steps,
                "spent_train": spend.spent("train"),
            }
            with log_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            print(json.dumps(row))
            if step % cfg.eval_every == 0 or step == max_steps:
                checkpoint(step)
                if should_stop(history, cfg.patience):
                    stop_reason = "no validation improvement"
                    break
            sig = hacking_signal(first_stats, stats) or collapse_signal(metrics_log, cfg)
            if sig:
                stop_reason = f"guard: {sig}"
                if not history or history[-1]["step"] != step:
                    checkpoint(step)  # a guard stop still leaves a scored checkpoint
                break
    except OverBudget as e:
        stop_reason = f"spend cap: {e}"
    finally:
        con.close()
    best = pick_checkpoint(history)
    state = {"config": asdict(cfg), "steps": step, "stop_reason": stop_reason, "best": best,
             "history": history}  # fmt: skip
    (M15 / "results" / "train_state.json").write_text(json.dumps(state, indent=1))
    print(json.dumps(state, indent=1))
    if best is not None and cfg.final_eval:
        print(json.dumps(evaluate(f"ckpt:{best['path']}", "eval", None, cfg.workers), indent=1))
    return 0
