"""M15 step 2: run the offline trainer toy (gridcase/rl/toy.py) over the candidate changes.

    uv run python experiments/rl_toy/run.py [--steps 200] [--seeds 5]

Writes experiments/rl_toy/results.md and results.json. Each row is one scheme averaged over
seeds: final exact Brier of the policy, the best constant on the grid and the direction/headroom
rule (the best grid answer per context), the correlation of the mean answer with direction
(down = 1; the truth is positive) and with headroom (the truth is negative), the number of grid
bins still carrying >= 2% mass, the fraction of degenerate groups over the last 5 steps, and the
mean answer after up- and down-moves (truth about 0.47 and 0.75).
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from dataclasses import asdict, replace
from pathlib import Path

from gridcase.rl.toy import OLD, Scheme, simulate

HERE = Path(__file__).parent


def variants() -> dict[str, Scheme]:
    new = replace(
        OLD, advantage_norm="batch_std", length_norm=True, answer_weight=4.0, stratify=True
    )
    return {
        "old (first run)": OLD,
        "old + batch_std": replace(OLD, advantage_norm="batch_std"),
        "old + group_std": replace(OLD, advantage_norm="group_std"),
        "old + stratify": replace(OLD, stratify=True),
        "old + length norm, answer x4": replace(OLD, length_norm=True, answer_weight=4.0),
        "old + KL base 0.03": replace(OLD, kl_coef=0.03),
        "old + KL base 0.1": replace(OLD, kl_coef=0.1),
        "old + KL base 0.3": replace(OLD, kl_coef=0.3),
        "old + entropy 0.03": replace(OLD, kl_coef=0.03, kl_target="uniform"),
        "old + entropy 0.1": replace(OLD, kl_coef=0.1, kl_target="uniform"),
        "old + entropy 0.3": replace(OLD, kl_coef=0.3, kl_target="uniform"),
        "old, groups of 16": replace(OLD, group_size=16),
        "new, no KL": new,
        "new + KL base 0.1": replace(new, kl_coef=0.1),
        "new + entropy 0.03": replace(new, kl_coef=0.03, kl_target="uniform"),
        "new + entropy 0.1": replace(new, kl_coef=0.1, kl_target="uniform"),
        "new + entropy 0.1, groups of 16": replace(
            new, kl_coef=0.1, kl_target="uniform", group_size=16
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=200)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--lr", type=float, nargs="+", default=[0.1, 0.2])
    args = ap.parse_args()
    rows = []
    for lr in args.lr:
        for name, sch in variants().items():
            cfg = replace(sch, lr=lr)
            rs = [simulate(cfg, steps=args.steps, seed=s) for s in range(args.seeds)]

            def m(key: str, rs: list = rs) -> float:  # noqa: B006
                return st.mean(getattr(r, key) for r in rs)

            rows.append({
                "lr": lr, "scheme": name, "brier": m("brier"),
                "brier_sd": st.pstdev(r.brier for r in rs),
                "brier_at_half": st.mean(r.curve[args.steps // 2]["brier"] for r in rs),
                "constant_brier": rs[0].constant_brier, "rule_brier": rs[0].rule_brier,
                "direction_corr": m("direction_corr"), "headroom_corr": m("headroom_corr"),
                "distinct": m("spike_p_distinct"), "degenerate": m("degenerate_frac_last"),
                "up": st.mean(r.mean_p["up"] for r in rs),
                "down": st.mean(r.mean_p["down"] for r in rs),
                "config": asdict(replace(sch, lr=lr)),
            })  # fmt: skip
            print(json.dumps({k: v for k, v in rows[-1].items() if k != "config"}))
    (HERE / "results.json").write_text(json.dumps(rows, indent=1))
    lines = [
        f"# Trainer toy: {args.steps} steps, {args.seeds} seeds per row",
        "",
        f"Best constant on the grid: {rows[0]['constant_brier']:.4f}. Direction/headroom rule: "
        f"{rows[0]['rule_brier']:.4f}. Truth: up-moves hold ~0.47, down-moves ~0.75.",
        "",
        "| lr | scheme | Brier (final) | ± | Brier (half) | dir corr | headroom corr | live bins "
        "| degenerate | up | down |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['lr']} | {r['scheme']} | {r['brier']:.4f} | {r['brier_sd']:.4f} | "
            f"{r['brier_at_half']:.4f} | {r['direction_corr']:+.2f} | {r['headroom_corr']:+.2f} | "
            f"{r['distinct']:.1f} | {r['degenerate']:.2f} | {r['up']:.2f} | {r['down']:.2f} |"
        )
    (HERE / "results.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
