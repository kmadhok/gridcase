"""M15 RL training: GRPO-style LoRA RL on Tinker over the train split.

Each step: save the current weights for sampling (on-policy), run `group_size` episodes per task
through the same agent loop and tools as the eval, score each with the reward
(gridcase/rl/reward.py), give each episode its reward minus its group's mean (groups with no
spread are skipped), turn every sampled turn into a Datum (the turn's full prompt plus its
sampled tokens, shifted by one; sampler logprobs and the advantage on the sampled tokens, zeros
on the prompt) and apply `importance_sampling` + Adam. Tinker calls verified against
tinker-docs.thinkingmachines.ai (SDK cheatsheet, quickstart RL example) on 2026-10-12.

A 3-step pilot measures dollars per step; the run then takes as many steps as the training
allowance affords (minus a reserve, at most max_steps). Every eval_every steps a sampler
checkpoint is scored on the validation split, never the eval split; the best validation Brier is
kept. It stops early after `patience` checkpoints without improvement, on a reward-hacking signal
(verifier drops more than doubled, or tool use collapsing to nothing), or at the spend cap.
"""

from __future__ import annotations

import json
import math
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

from gridcase import config


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


@dataclass
class DatumSpec:
    """A Tinker Datum as plain lists (built offline, converted at the API boundary)."""

    input_tokens: list[int]
    target_tokens: list[int]
    logprobs: list[float]
    advantages: list[float]


def group_advantages(rewards: list[float]) -> list[float]:
    """Reward minus the group mean; all zeros when the group has no spread."""
    if not rewards or max(rewards) - min(rewards) < 1e-12:
        return [0.0] * len(rewards)
    m = sum(rewards) / len(rewards)
    return [r - m for r in rewards]


def episode_datums(raw_turns: list[dict[str, Any]], advantage: float) -> list[DatumSpec]:
    out = []
    for t in raw_turns:
        if not t or not t.get("tokens"):
            continue
        prompt, sampled = list(t["prompt_tokens"]), list(t["tokens"])
        logprobs = list(t.get("logprobs") or [])
        if len(logprobs) != len(sampled):
            continue  # no logprobs for this turn: cannot be importance-weighted
        full = prompt + sampled
        n_prompt = len(prompt) - 1
        out.append(
            DatumSpec(
                input_tokens=full[:-1],
                target_tokens=full[1:],
                logprobs=[0.0] * n_prompt + logprobs,
                advantages=[0.0] * n_prompt + [advantage] * len(sampled),
            )
        )
    return out


def steps_affordable(remaining_usd: float, usd_per_step: float, reserve: float, cap: int) -> int:
    if usd_per_step <= 0:
        return cap
    return max(0, min(cap, math.floor((remaining_usd - reserve) / usd_per_step)))


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


def make_batch(tasks: list[Any], n: int, spike_share: float, rng: random.Random) -> list[Any]:
    spikes = [t for t in tasks if t.kind == "spike_hold"]
    res = [t for t in tasks if t.kind == "resolve"]
    k = min(len(spikes), round(n * spike_share))
    return rng.sample(spikes, k) + rng.sample(res, min(len(res), n - k))


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

    cfg = TrainConfig(tasks_per_step=args.tasks_per_step, group_size=args.group_size)
    rng = random.Random(cfg.seed)
    train, answers, _ = _load("train")
    val, val_answers, rates = _load("validation")
    spend = Spend(config.RL_DIR / "spend.jsonl")
    con = connect(config.BACKFILL_DB, read_only=True)
    service = tinker.ServiceClient()
    tc = service.create_lora_training_client(base_model=config.RL_BASE_MODEL, rank=cfg.rank)
    train_price = TINKER_PRICES.get(config.RL_BASE_MODEL, (0.3, 1.0, 1.0))[2]
    log_path = config.RL_DIR / "train_log.jsonl"
    history: list[dict[str, Any]] = []
    first_stats: dict[str, float] | None = None
    max_steps = cfg.max_steps
    start_spent = spend.spent("train")
    lock = threading.Lock()
    step = 0
    stop_reason = "max steps"
    try:
        while step < max_steps:
            sampler = tc.save_weights_and_get_sampling_client(name=f"step-{step}")
            agent = TinkerAgent(sampling_client=sampler, phase="train", temperature=cfg.temperature)
            batch = make_batch(train, cfg.tasks_per_step, cfg.spike_share, rng)
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
            data: list[DatumSpec] = []
            rewards_all, dropped, calls = [], 0, 0
            for t in batch:
                eps = results[t.task_id]
                y = answers[t.task_id].y
                rw = [reward_for(e.probability, y, e.malformed, e.penalised) for e in eps]
                rewards_all += rw
                dropped += sum(len(e.dropped) for e in eps)
                calls += sum(e.tool_calls for e in eps)
                for e, adv in zip(eps, group_advantages(rw), strict=True):
                    if adv != 0.0:
                        data += episode_datums(e.raw_turns, adv)
            n_eps = max(1, len(jobs))
            stats = {"dropped": dropped / n_eps, "tool_calls": calls / n_eps}
            first_stats = first_stats or stats
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
            row = {
                "step": step,
                "mean_reward": sum(rewards_all) / len(rewards_all),
                "datums": len(data),
                **stats,
                "spent_train": spend.spent("train"),
            }
            with log_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            print(json.dumps(row))
            step += 1
            if step == cfg.pilot_steps:
                per_step = (spend.spent("train") - start_spent) / cfg.pilot_steps
                left = spend.remaining("train")
                max_steps = cfg.pilot_steps + steps_affordable(
                    left, per_step, cfg.reserve_usd, cfg.max_steps
                )
                print(json.dumps({"pilot_usd_per_step": per_step, "planned_steps": max_steps}))
            if step % cfg.eval_every == 0 or step == max_steps:
                ck = tc.save_weights_for_sampler(
                    f"ckpt-{step}", ttl_seconds=cfg.ttl_days * 86400
                ).result()
                vagent = TinkerAgent(model_path=ck.path, phase="small_eval")
                out = M15 / "results" / f"val_step{step}_validation.json"
                res = run_eval(f"val_step{step}", val, val_answers, rates, out, conn=con,
                               model=vagent, spend=spend, workers=cfg.workers)  # fmt: skip
                history.append(
                    {"step": step, "path": ck.path, "val_brier": res["summary"]["all"]["brier"]}
                )
                (M15 / "results" / "train_history.json").write_text(json.dumps(history, indent=1))
                if should_stop(history, cfg.patience):
                    stop_reason = "no validation improvement"
                    break
            sig = hacking_signal(first_stats, stats)
            if sig:
                stop_reason = f"reward-hacking signal: {sig}"
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
    if best is not None:
        print(json.dumps(evaluate(f"ckpt:{best['path']}", "eval", None, cfg.workers), indent=1))
    return 0
