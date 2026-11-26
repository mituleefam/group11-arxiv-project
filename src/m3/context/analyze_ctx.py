"""Milestone 3, context experiment - step 5: which context features matter (cross-validation on train only).

    python src/m3/context/analyze_ctx.py --work WORK [--ctx WORK/context] [--folds 5] [--seed 11] [--repeats 2]

1. Alone: each context feature as the only score (higher is better, lower for
   ranks, gaps, ages), MRR@5 on train, and its mean on the right / other
   candidates -> CTX/ctx_feature_report.csv
2. Group ablation (the folds of train_ctx.py): the context model (`ctx`)
   without one group of ctx_features.CTX_GROUPS at a time (MRR@5 on the
   entries with a context and on the hard ones), and the `meta+ctx` model
   without one context group (MRR@5 with no link on the hard entries, where
   the context matters) -> CTX/ctx_ablation_cv.csv
3. Permutation importance of the context model: the fold models score their
   held-out fold with one feature shuffled (mean of --repeats)
   -> CTX/ctx_importance_cv.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "modeling"))
from ctx_features import CTX_FEATURES, CTX_GROUPS  # noqa: E402
from pairs import COMPACT, Pairs, mrr_at_5  # noqa: E402
from models import score  # noqa: E402
from train import fit, hard_groups, make_folds  # noqa: E402

LOWER_BETTER = ("rank_", "gap_", "cand_age", "desc_missing")


def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--ctx", type=Path)
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--repeats", type=int, default=2)
    args = p.parse_args(argv)
    ctx = args.ctx or args.work / "context"
    from features import FEATURES  # noqa: E402
    pairs = Pairs(ctx, columns=list(dict.fromkeys(FEATURES + CTX_FEATURES)))
    col = {c: i for i, c in enumerate(pairs.columns)}
    tr = pairs.rows("train")
    y, g = pairs.y[tr], pairs.gidx[tr]
    fold = make_folds(pairs, tr, args.folds, args.seed)
    has = pairs.X[tr, col["ctx_missing"]] == 0
    hard = hard_groups(pairs, tr)

    # 1. alone
    rows = []
    for f in CTX_FEATURES:
        x = pairs.X[tr, col[f]]
        s = np.nan_to_num(-x if f.startswith(LOWER_BETTER) else x, nan=-1e9)
        group = next(k for k, v in CTX_GROUPS.items() if f in v)
        rows.append({"feature": f, "group": group, "mrr5_alone": mrr_at_5(s[has], y[has], g[has])["mrr5"],
                     "mean_yes": round(float(np.nanmean(x[y == 1])), 4),
                     "mean_no": round(float(np.nanmean(x[y == 0])), 4),
                     "missing": round(float(np.isnan(x).mean()), 4)})
    write_csv(ctx / "ctx_feature_report.csv", rows)
    print("alone:", ", ".join(f"{r['feature']} {r['mrr5_alone']:.3f}" for r in rows), flush=True)

    def cv(features, share):
        s, s_nl, models = np.zeros(len(tr)), np.zeros(len(tr)), {}
        for k in range(args.folds):
            m = fit("lgbm", pairs, tr[fold != k], features, share, args.seed)
            held = fold == k
            s[held] = score(m, "lgbm", pairs.matrix(tr[held], features))
            s_nl[held] = score(m, "lgbm", pairs.matrix(tr[held], features, True))
            models[k] = m
        return s, s_nl, models

    def mrr(s, m):
        return mrr_at_5(s[m], y[m], g[m])["mrr5"]

    # 2. group ablation
    abl = []
    base_s, _, base_models = cv(CTX_FEATURES, 0.0)
    b = (mrr(base_s, has), mrr(base_s, has & hard))
    abl.append({"model": "ctx", "removed": "(none)", "features": len(CTX_FEATURES), "mrr5": b[0], "mrr5_hard": b[1],
                "drop": 0.0, "drop_hard": 0.0})
    print(f"ctx: {b[0]:.4f}, hard {b[1]:.4f}", flush=True)
    for grp, fs in CTX_GROUPS.items():
        keep = [f for f in CTX_FEATURES if f not in fs]
        s, _, _ = cv(keep, 0.0)
        r = (mrr(s, has), mrr(s, has & hard))
        abl.append({"model": "ctx", "removed": grp, "features": len(keep), "mrr5": r[0], "mrr5_hard": r[1],
                    "drop": round(b[0] - r[0], 4), "drop_hard": round(b[1] - r[1], 4)})
        print(f"  ctx without {grp:11}: {r[0]:.4f} ({b[0] - r[0]:+.4f}), hard {r[1]:.4f} ({b[1] - r[1]:+.4f})",
              flush=True)
    all_ = np.ones(len(tr), bool)
    _, mc_nl, _ = cv(COMPACT + CTX_FEATURES, 0.5)
    b2 = (mrr(mc_nl, all_), mrr(mc_nl, hard))
    abl.append({"model": "meta+ctx (no link)", "removed": "(none)", "features": len(COMPACT + CTX_FEATURES),
                "mrr5": b2[0], "mrr5_hard": b2[1], "drop": 0.0, "drop_hard": 0.0})
    for grp, fs in list(CTX_GROUPS.items()) + [("all context", CTX_FEATURES)]:
        keep = [f for f in COMPACT + CTX_FEATURES if f not in fs]
        _, s_nl, _ = cv(keep, 0.5)
        r = (mrr(s_nl, all_), mrr(s_nl, hard))
        abl.append({"model": "meta+ctx (no link)", "removed": grp, "features": len(keep), "mrr5": r[0],
                    "mrr5_hard": r[1], "drop": round(b2[0] - r[0], 4), "drop_hard": round(b2[1] - r[1], 4)})
        print(f"  meta+ctx without {grp:11}: hard no link {r[1]:.4f} ({b2[1] - r[1]:+.4f})", flush=True)
    write_csv(ctx / "ctx_ablation_cv.csv", abl)

    # 3. permutation importance of the context model
    X = pairs.matrix(tr, CTX_FEATURES)
    rng = np.random.default_rng(args.seed)
    imp = []
    for j, f in enumerate(CTX_FEATURES):
        d, dh = [], []
        for _ in range(args.repeats):
            Xp = X.copy()
            Xp[:, j] = Xp[rng.permutation(len(Xp)), j]
            s = np.zeros(len(tr))
            for k, m in base_models.items():
                held = fold == k
                s[held] = score(m, "lgbm", Xp[held])
            d.append(b[0] - mrr(s, has))
            dh.append(b[1] - mrr(s, has & hard))
        imp.append({"feature": f, "group": next(k for k, v in CTX_GROUPS.items() if f in v),
                    "mrr5_drop": round(float(np.mean(d)), 4), "mrr5_drop_hard": round(float(np.mean(dh)), 4)})
        print(f"  shuffled {f:16}: {imp[-1]['mrr5_drop']:+.4f}, hard {imp[-1]['mrr5_drop_hard']:+.4f}", flush=True)
    imp.sort(key=lambda r: -r["mrr5_drop"])
    write_csv(ctx / "ctx_importance_cv.csv", imp)
    print(f"-> {ctx / 'ctx_feature_report.csv'}, ctx_ablation_cv.csv, ctx_importance_cv.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
