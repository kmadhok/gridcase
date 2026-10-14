"""Free check (guide D3): does the price/flow path predict a spike holding beyond q?

Reads data/rl/diagnosis/free/path_features.parquet (free_path_features.py). Fits on train only
(4,000 spike tasks), with cluster-aware CV (GroupKFold on cluster_id) for every tuning choice;
scores once on eval (400) and validation (150). Holdout never loaded.

    uv run --with scikit-learn --with pandas --with pyarrow \\
        python experiments/m15_diagnosis/free_path_model.py

Writes data/rl/diagnosis/free/path_model.json and path_predictions.parquet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from gridcase.rl.reward import ece, paired_bootstrap  # noqa: E402

FREE = ROOT / "data/rl/diagnosis/free"
EPS = 0.01  # reward.clip
FOLDS = 5
SEED = 0


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def brier_vec(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    return (np.clip(p, EPS, 1 - EPS) - y) ** 2


def l10(x: pd.Series, floor: float = 1e-3) -> pd.Series:
    return np.log10(x.clip(lower=0) + floor)


# feature groups (names in the design matrix) -> raw construction
def design(d: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    X = pd.DataFrame(index=d.index)
    X["lq"] = logit(d["q"].to_numpy())
    X["up"] = d["up"].astype(float)
    groups: dict[str, list[str]] = {"base": ["lq", "up"]}

    def add(group: str, name: str, col: pd.Series) -> None:
        X[name] = col.astype(float)
        groups.setdefault(group, []).append(name)

    add("volatility", "log_vol10m_24h", l10(d["vol10m_24h"]))
    add("volatility", "log_vol10m_7d", l10(d["vol10m_7d"]))
    add("volatility", "log_vol1h_24h", l10(d["vol1h_24h"]))
    add("volatility", "log_vol1h_7d", l10(d["vol1h_7d"]))
    add("volatility", "log_vol1h_7d_pre", l10(d["vol1h_7d_pre"]))
    add("volatility", "log_move_z", np.log10(d["move_z"].clip(lower=0.05)))
    add("volatility", "log_range_7d_pre", l10(d["range_7d_pre"], 1e-2))
    add("volatility", "flat_before", d["flat_before"])
    add("speed", "share_last1h", d["share_last1h"].clip(-1, 2))
    add("speed", "n_steps80", d["n_steps80"])
    add("speed", "max_step_share", d["max_step_share"].clip(0, 2))
    add("speed", "single_jump", d["single_jump"])
    add("speed", "path_efficiency", d["path_efficiency"])
    add("launch", "dist50_before", d["dist50_before"])
    add("launch", "before_at_50", d["before_at_50"])
    add("launch", "young12h", d["young12h"])
    add("launch", "log_age_h", np.log10(d["age_hours"].clip(lower=0.1)))
    add("launch", "log_points_7d", np.log10(d["n_points_7d"].clip(lower=1)))
    add("time_to_end", "log_hours_to_end", np.log10(d["hours_to_end"].clip(lower=1)))
    add("move_size", "abs_move", d["abs_move"])
    add("flow", "log_trades_6h", d["log_trades_6h"])
    add("flow", "log_usd_6h", d["log_usd_6h"])
    add("flow", "log_trade_ratio", d["log_trade_ratio"])
    add("flow", "log_usd_ratio", d["log_usd_ratio"])
    add("flow", "with_move_share", d["with_move_share"])
    add("flow", "buy_share_6h", d["buy_share_6h"])
    add("siblings", "log1p_siblings", np.log1p(d["n_siblings"]))
    add("siblings", "sib_same_share", d["sib_same_share"])
    add("siblings", "sib_opp_share", d["sib_opp_share"])
    add("siblings", "neg_risk", d["neg_risk"])
    # direction interactions for the two features most likely to differ by direction
    add("interactions", "lq_x_up", X["lq"] * X["up"])
    add("interactions", "young12h_x_up", X["young12h"] * X["up"])
    return X, groups


class Imputer:
    """Train medians plus a missing flag for any column with NaN in train."""

    def fit(self, X: pd.DataFrame) -> Imputer:
        self.med = X.median()
        self.flag = [c for c in X.columns if X[c].isna().any()]
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        out = X.copy()
        for c in self.flag:
            out[f"{c}_missing"] = out[c].isna().astype(float)
        return out.fillna(self.med)


def lr_model(C: float) -> Any:
    return make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=5000))


def hgb_model(depth: int, lr: float, iters: int) -> Any:
    return HistGradientBoostingClassifier(
        max_depth=depth,
        learning_rate=lr,
        max_iter=iters,
        min_samples_leaf=60,
        l2_regularization=1.0,
        random_state=SEED,
    )


def oof(make: Any, X: pd.DataFrame, y: np.ndarray, groups: np.ndarray, impute: bool) -> np.ndarray:
    out = np.zeros(len(y))
    for tr, te in GroupKFold(FOLDS).split(X, y, groups):
        Xtr, Xte = X.iloc[tr], X.iloc[te]
        if impute:
            imp = Imputer().fit(Xtr)
            Xtr, Xte = imp.transform(Xtr), imp.transform(Xte)
        m = make().fit(Xtr, y[tr])
        out[te] = m.predict_proba(Xte)[:, 1]
    return out


def fit_predict(
    make: Any, Xtr: pd.DataFrame, y: np.ndarray, Xs: list[pd.DataFrame], impute: bool
) -> tuple[Any, list[np.ndarray], list[str]]:
    if impute:
        imp = Imputer().fit(Xtr)
        Xtr = imp.transform(Xtr)
        Xs = [imp.transform(x) for x in Xs]
    m = make().fit(Xtr, y)
    return m, [m.predict_proba(x)[:, 1] for x in Xs], list(Xtr.columns)


def calib(p: np.ndarray, y: np.ndarray) -> dict[str, float]:
    """ECE, and the slope/intercept of y ~ logit(p) (1 and 0 when calibrated)."""
    m = LogisticRegression(C=1e6, max_iter=5000).fit(logit(p).reshape(-1, 1), y)
    return {
        "ece": ece(list(p), list(y)),
        "slope": float(m.coef_[0][0]),
        "intercept": float(m.intercept_[0]),
        "mean_p": float(p.mean()),
    }


def reliability(p: np.ndarray, y: np.ndarray, bins: int = 5) -> list[dict[str, float]]:
    qs = np.quantile(p, np.linspace(0, 1, bins + 1))
    idx = np.clip(np.searchsorted(qs, p, side="right") - 1, 0, bins - 1)
    return [
        {
            "bin": int(b),
            "n": int((idx == b).sum()),
            "mean_p": float(p[idx == b].mean()),
            "y_rate": float(y[idx == b].mean()),
        }
        for b in range(bins)
        if (idx == b).any()
    ]


def score(name: str, d: pd.DataFrame, preds: dict[str, np.ndarray]) -> dict[str, Any]:
    y = d["y"].to_numpy()
    ids = list(d["task_id"])
    cl = dict(zip(ids, d["cluster_id"], strict=True))
    bq = dict(zip(ids, brier_vec(preds["q"], y), strict=True))
    out: dict[str, Any] = {
        "n": len(y),
        "clusters": int(d["cluster_id"].nunique()),
        "y_rate": float(y.mean()),
        "models": {},
    }
    for m, p in preds.items():
        b = brier_vec(p, y)
        bm = dict(zip(ids, b, strict=True))
        diff, lo, hi = paired_bootstrap(bm, bq, cl, draws=4000, seed=SEED)
        row: dict[str, Any] = {"brier": float(b.mean()), "vs_q": [diff, lo, hi]}
        if m not in ("q", "base_lr"):
            bb = dict(zip(ids, brier_vec(preds["base_lr"], y), strict=True))
            row["vs_base"] = list(paired_bootstrap(bm, bb, cl, draws=4000, seed=SEED))
        row |= calib(p, y)
        for lab, mask in (("up", d["up"].to_numpy() == 1), ("down", d["up"].to_numpy() == 0)):
            row[f"brier_{lab}"] = float(b[mask].mean())
            row[f"brier_q_{lab}"] = float(brier_vec(preds["q"][mask], y[mask]).mean())
        row["reliability"] = reliability(p, y)
        out["models"][m] = row
    return out


def main() -> None:
    d = pd.read_parquet(FREE / "path_features.parquet")
    tr, ev, va = (d[d.split == s].reset_index(drop=True) for s in ("train", "eval", "validation"))
    print(f"train {len(tr)} eval {len(ev)} validation {len(va)}")
    Xtr, groups = design(tr)
    Xev, _ = design(ev)
    Xva, _ = design(va)
    ytr = tr["y"].to_numpy()
    g = tr["cluster_id"].to_numpy()
    base_cols = groups["base"]
    path_cols = list(Xtr.columns)
    report: dict[str, Any] = {"features": groups, "cv": {}}

    # --- tuning on train, cluster-aware CV ---
    q_tr = tr["q"].to_numpy()
    report["cv"]["q"] = float(brier_vec(q_tr, ytr).mean())
    base_oof = oof(lambda: lr_model(1e4), Xtr[base_cols], ytr, g, False)
    report["cv"]["base_lr"] = float(brier_vec(base_oof, ytr).mean())
    lr_grid = {}
    for C in (0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 10.0):
        lr_grid[C] = float(
            brier_vec(oof(lambda C=C: lr_model(C), Xtr[path_cols], ytr, g, True), ytr).mean()
        )
    best_C = min(lr_grid, key=lambda c: lr_grid[c])
    report["cv"]["path_lr_grid"] = {str(k): v for k, v in lr_grid.items()}
    hgb_grid = {}
    for depth, lr, it in (
        (2, 0.05, 100),
        (2, 0.05, 200),
        (3, 0.03, 150),
        (3, 0.05, 200),
        (2, 0.03, 300),
    ):
        hgb_grid[(depth, lr, it)] = float(
            brier_vec(
                oof(lambda a=depth, b=lr, c=it: hgb_model(a, b, c), Xtr[path_cols], ytr, g, False),
                ytr,
            ).mean()
        )
    best_h = min(hgb_grid, key=lambda k: hgb_grid[k])
    report["cv"]["path_hgb_grid"] = {str(k): v for k, v in hgb_grid.items()}
    report["cv"]["best_C"], report["cv"]["best_hgb"] = best_C, list(best_h)
    path_oof = oof(lambda: lr_model(best_C), Xtr[path_cols], ytr, g, True)
    hgb_oof = oof(lambda: hgb_model(*best_h), Xtr[path_cols], ytr, g, False)
    # train out-of-fold paired differences vs q (n = 4,000: the best-powered estimate we have)
    ids = list(tr["task_id"])
    cl = dict(zip(ids, tr["cluster_id"], strict=True))
    bq = dict(zip(ids, brier_vec(q_tr, ytr), strict=True))
    report["cv"]["oof_vs_q"] = {
        m: list(
            paired_bootstrap(
                dict(zip(ids, brier_vec(p, ytr), strict=True)), bq, cl, draws=2000, seed=SEED
            )
        )
        for m, p in (("base_lr", base_oof), ("path_lr", path_oof), ("path_hgb", hgb_oof))
    }
    report["cv"]["clusters"] = int(tr["cluster_id"].nunique())

    # drop-one-group importance on train CV (no eval data involved)
    full = float(brier_vec(path_oof, ytr).mean())
    drop = {}
    for grp, cols in groups.items():
        if grp == "base":
            continue
        keep = [c for c in path_cols if c not in cols]
        p = oof(lambda: lr_model(best_C), Xtr[keep], ytr, g, True)
        drop[grp] = float(brier_vec(p, ytr).mean()) - full
    only = {}
    for grp, cols in groups.items():
        if grp == "base":
            continue
        p = oof(lambda: lr_model(best_C), Xtr[base_cols + cols], ytr, g, True)
        only[grp] = float(brier_vec(p, ytr).mean()) - report["cv"]["base_lr"]
    report["cv"]["drop_group_delta"] = drop
    report["cv"]["base_plus_group_delta"] = only

    # --- final fits on all train, score eval and validation ---
    base_m, (b_ev, b_va), _ = fit_predict(
        lambda: lr_model(1e4), Xtr[base_cols], ytr, [Xev[base_cols], Xva[base_cols]], False
    )
    path_m, (p_ev, p_va), cols = fit_predict(
        lambda: lr_model(best_C), Xtr[path_cols], ytr, [Xev[path_cols], Xva[path_cols]], True
    )
    _, (h_ev, h_va), _ = fit_predict(
        lambda: hgb_model(*best_h), Xtr[path_cols], ytr, [Xev[path_cols], Xva[path_cols]], False
    )
    lr = path_m.named_steps["logisticregression"]
    report["base_coef"] = dict(zip(base_cols, map(float, base_m[-1].coef_[0]), strict=True))
    report["base_coef"]["intercept"] = float(base_m[-1].intercept_[0])
    coefs = dict(zip(cols, map(float, lr.coef_[0]), strict=True))
    report["path_coef_std"] = dict(sorted(coefs.items(), key=lambda kv: -abs(kv[1])))
    for name, dd, preds in (
        ("eval", ev, {"q": ev["q"].to_numpy(), "base_lr": b_ev, "path_lr": p_ev, "path_hgb": h_ev}),
        (
            "validation",
            va,
            {"q": va["q"].to_numpy(), "base_lr": b_va, "path_lr": p_va, "path_hgb": h_va},
        ),
    ):
        report[name] = score(name, dd, preds)

    # permutation importance on eval (Brier rise when a group's columns are shuffled together)
    imp = Imputer().fit(Xtr[path_cols])
    Xe = imp.transform(Xev[path_cols])
    yev = ev["y"].to_numpy()
    base_b = float(brier_vec(path_m.predict_proba(Xe)[:, 1], yev).mean())
    rng = np.random.default_rng(SEED)
    perm = {}
    for grp, gcols in groups.items():
        cs = [c for c in Xe.columns if any(c == x or c == f"{x}_missing" for x in gcols)]
        rises = []
        for _ in range(30):
            Xp = Xe.copy()
            order = rng.permutation(len(Xp))
            Xp[cs] = Xp[cs].to_numpy()[order]
            rises.append(float(brier_vec(path_m.predict_proba(Xp)[:, 1], yev).mean()) - base_b)
        perm[grp] = float(np.mean(rises))
    report["eval_permutation_delta"] = perm

    (FREE / "path_model.json").write_text(json.dumps(report, indent=2, default=str))
    pd.concat(
        [
            ev.assign(p_base=b_ev, p_path=p_ev, p_hgb=h_ev),
            va.assign(p_base=b_va, p_path=p_va, p_hgb=h_va),
        ]
    )[["task_id", "split", "cluster_id", "y", "q", "up", "p_base", "p_path", "p_hgb"]].to_parquet(
        FREE / "path_predictions.parquet", index=False
    )

    print(json.dumps(report["cv"], indent=1, default=str))
    for s in ("eval", "validation"):
        print(
            s, report[s]["n"], "clusters", report[s]["clusters"], "y", round(report[s]["y_rate"], 3)
        )
        for m, r in report[s]["models"].items():
            vs = r["vs_q"]
            print(
                f"  {m:9s} brier {r['brier']:.4f}  vs q {vs[0]:+.4f} [{vs[1]:+.4f}, {vs[2]:+.4f}]"
                f"  ece {r['ece']:.3f} slope {r['slope']:.2f} int {r['intercept']:+.2f}"
                f"  up {r['brier_up']:.4f} down {r['brier_down']:.4f}"
                + (
                    f"  vs base {r['vs_base'][0]:+.4f} "
                    f"[{r['vs_base'][1]:+.4f}, {r['vs_base'][2]:+.4f}]"
                    if "vs_base" in r
                    else ""
                )
            )
    print(
        "coef", json.dumps({k: round(v, 3) for k, v in list(report["path_coef_std"].items())[:15]})
    )
    print("perm", json.dumps({k: round(v, 4) for k, v in perm.items()}))
    print("base coef", report["base_coef"])


if __name__ == "__main__":
    main()
