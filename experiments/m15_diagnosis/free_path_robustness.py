"""Robustness checks for free_path_model.py: is the train-CV gain stable over time, and where on
eval does it go? Train/eval/validation only; holdout never loaded.

    uv run --with scikit-learn --with pandas --with pyarrow \\
        python experiments/m15_diagnosis/free_path_robustness.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from free_path_model import FREE, Imputer, brier_vec, design, lr_model  # noqa: E402

from gridcase.rl.reward import paired_bootstrap  # noqa: E402

C = json.loads((FREE / "path_model.json").read_text())["cv"]["best_C"]


def boot(a: np.ndarray, b: np.ndarray, d: pd.DataFrame) -> list[float]:
    ids = list(d["task_id"])
    return list(
        paired_bootstrap(
            dict(zip(ids, a, strict=True)),
            dict(zip(ids, b, strict=True)),
            dict(zip(ids, d["cluster_id"], strict=True)),
            2000,
            0,
        )
    )


def fit(tr: pd.DataFrame, te: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    Xtr, g = design(tr)
    Xte, _ = design(te)
    base = lr_model(1e4).fit(Xtr[g["base"]], tr["y"]).predict_proba(Xte[g["base"]])[:, 1]
    imp = Imputer().fit(Xtr)
    path = lr_model(C).fit(imp.transform(Xtr), tr["y"]).predict_proba(imp.transform(Xte))[:, 1]
    return base, path


def main() -> None:
    d = pd.read_parquet(FREE / "path_features.parquet")
    d["as_of"] = d["task_id"].str.rsplit(":", n=1).str[1].astype(int)
    out: dict[str, object] = {}
    tr = d[d.split == "train"].sort_values("as_of").reset_index(drop=True)
    # forward-chained: fit on the earliest k quarters of train, test on the next quarter
    qs = np.array_split(np.arange(len(tr)), 4)
    fwd = []
    for k in range(1, 4):
        a, b = tr.iloc[np.concatenate(qs[:k])], tr.iloc[qs[k]].reset_index(drop=True)
        base, path = fit(a, b)
        y, q = b["y"].to_numpy(), b["q"].to_numpy()
        bq, bb, bp = brier_vec(q, y), brier_vec(base, y), brier_vec(path, y)
        fwd.append(
            {
                "test_from": str(pd.to_datetime(b["as_of"].min(), unit="s")),
                "test_to": str(pd.to_datetime(b["as_of"].max(), unit="s")),
                "n": len(b),
                "brier_q": float(bq.mean()),
                "base_vs_q": boot(bb, bq, b),
                "path_vs_q": boot(bp, bq, b),
                "path_vs_base": boot(bp, bb, b),
            }
        )
    out["forward_chained_train"] = fwd
    for s in ("validation", "eval"):
        x = d[d.split == s]
        out[f"{s}_dates"] = [
            str(pd.to_datetime(x["as_of"].min(), unit="s")),
            str(pd.to_datetime(x["as_of"].max(), unit="s")),
        ]
    out["train_dates"] = [
        str(pd.to_datetime(tr["as_of"].min(), unit="s")),
        str(pd.to_datetime(tr["as_of"].max(), unit="s")),
    ]

    # eval subsets: where the full-train path model gains or loses vs q
    ev = d[d.split == "eval"].reset_index(drop=True)
    base, path = fit(tr, ev)
    y, q = ev["y"].to_numpy(), ev["q"].to_numpy()
    sub = {}
    for name, mask in {
        "up": ev.up == 1,
        "down": ev.up == 0,
        "young12h": ev.young12h == 1,
        "not_young": ev.young12h == 0,
        "before_at_50": ev.before_at_50 == 1,
        "single_jump": ev.single_jump == 1,
        "gradual": ev.single_jump == 0,
        "share_last1h>=0.8": ev.share_last1h >= 0.8,
        "share_last1h<0.8": ev.share_last1h < 0.8,
        "Sports": ev.category == "Sports",
        "not Sports": ev.category != "Sports",
    }.items():
        m = mask.to_numpy()
        sub[name] = {
            "n": int(m.sum()),
            "y": float(y[m].mean()),
            "q": float(q[m].mean()),
            "p_path": float(path[m].mean()),
            "brier_q": float(brier_vec(q[m], y[m]).mean()),
            "path_minus_q": float((brier_vec(path[m], y[m]) - brier_vec(q[m], y[m])).mean()),
        }
    out["eval_subsets"] = sub
    # the same subsets' hold rates on train vs eval vs validation (does the pattern drift?)
    rates = {}
    for s in ("train", "validation", "eval"):
        x = d[d.split == s]
        rates[s] = {
            "y": float(x.y.mean()),
            "q": float(x.q.mean()),
            "y_last1h>=0.8": float(x[x.share_last1h >= 0.8].y.mean()),
            "y_last1h<0.8": float(x[x.share_last1h < 0.8].y.mean()),
            "share_last1h>=0.8": float((x.share_last1h >= 0.8).mean()),
            "y-q last1h>=0.8": float((x.y - x.q)[x.share_last1h >= 0.8].mean()),
            "y-q last1h<0.8": float((x.y - x.q)[x.share_last1h < 0.8].mean()),
            "y-q young12h": float((x.y - x.q)[x.young12h == 1].mean()),
            "y-q not young": float((x.y - x.q)[x.young12h == 0].mean()),
            "y-q up": float((x.y - x.q)[x.up == 1].mean()),
            "y-q down": float((x.y - x.q)[x.up == 0].mean()),
        }
    out["rates_by_split"] = rates
    # parsimonious models: base plus one stable group, fitted on all train
    pars = {}
    Xtr, g = design(tr)
    for label, grps in {
        "base+speed": ["speed"],
        "base+speed+volatility": ["speed", "volatility"],
        "base+speed+launch": ["speed", "launch"],
    }.items():
        cols = g["base"] + [c for k in grps for c in g[k]]
        imp = Imputer().fit(Xtr[cols])
        m = lr_model(C).fit(imp.transform(Xtr[cols]), tr["y"])
        pars[label] = {}
        for sname in ("validation", "eval"):
            x = d[d.split == sname].reset_index(drop=True)
            Xs, _ = design(x)
            p = m.predict_proba(imp.transform(Xs[cols]))[:, 1]
            yy, qq = x["y"].to_numpy(), x["q"].to_numpy()
            bb = lr_model(1e4).fit(Xtr[g["base"]], tr["y"]).predict_proba(Xs[g["base"]])[:, 1]
            pars[label][sname] = {
                "brier": float(brier_vec(p, yy).mean()),
                "vs_q": boot(brier_vec(p, yy), brier_vec(qq, yy), x),
                "vs_base": boot(brier_vec(p, yy), brier_vec(bb, yy), x),
            }
    out["parsimonious"] = pars
    (FREE / "path_robustness.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
