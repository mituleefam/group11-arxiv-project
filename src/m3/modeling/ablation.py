"""Milestone 3, modelling - which features matter, measured by cross-validation on train (never on test).

    python src/m3/modeling/ablation.py --work WORK [--folds 5] [--repeats 3] [--combos]

Uses the configuration of WORK/models/chosen.json (model kind and link-mask
share) and the folds of train.py (by publication, same seed). The valid set is
not used: every scorer reaches MRR@5 = 1 on it, it cannot tell anything apart.

1. Group ablation: the configuration is cross-validated without one feature
   group (link, title, author, year, venue, context) at a time; the drop of
   out-of-fold MRR@5 (all, no link, hard entries) says what the group brings.
2. Permutation importance: models of the folds are scored on their held-out
   fold, with the link features masked (the entry names no identifier, the
   case where the model matters) and one feature shuffled (mean of --repeats
   shuffles); the drop of out-of-fold MRR@5 (all and hard) is the importance.
Groups that encode the same signal hide each other (title and context both
encode title similarity): "title+context" removes both.
3. --combos (instead of 1 and 2): larger groups removed together, COMBOS -
   title+link, title+context+link (what is left: author, year, venue) and
   year+author+venue (what is left: link, title, context).

Writes WORK/ablation_cv.csv and WORK/importance_cv.csv; with --combos,
WORK/ablation_combos_cv.csv only.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pairs import FEATURE_GROUPS, Pairs, mrr_at_5  # noqa: E402
from models import score  # noqa: E402
from train import cv_scores, fit, hard_groups, make_folds, oof_metrics  # noqa: E402

COMBOS = {"title+link": ["title", "link"], "title+context+link": ["title", "context", "link"],
          "year+author+venue": ["year", "author", "venue"]}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--combos", action="store_true", help="remove the COMBOS groups together, no importance")
    args = p.parse_args(argv)
    folder = args.work / "models"
    chosen = json.loads((folder / "chosen.json").read_text(encoding="utf-8"))["model"]
    meta = json.loads((folder / f"{chosen}.json").read_text(encoding="utf-8"))
    kind, share, features = meta["kind"], meta["mask_share"], meta["features"]
    pairs = Pairs(args.work)
    tr = pairs.rows("train")
    fold = make_folds(pairs, tr, args.folds, args.seed)
    hard = hard_groups(pairs, tr)

    def summary(s, s_nl):  # all, no link, hard with no link (the honest measure: see train.py)
        m = oof_metrics(pairs, tr, s, s_nl, hard)
        return m["all"]["mrr5"], m["all_nolink"]["mrr5"], m["hard_nolink"]["mrr5"]

    base = summary(*cv_scores(pairs, tr, fold, kind, share, features, args.seed))
    print(f"{chosen} ({len(features)} features): out-of-fold MRR@5 all {base[0]:.4f}, no link {base[1]:.4f}, "
          f"hard no link {base[2]:.4f}", flush=True)
    rows = [{"removed": "(none)", "features": len(features), "mrr5": base[0], "mrr5_nolink": base[1],
             "mrr5_hard": base[2], "drop": 0.0, "drop_nolink": 0.0, "drop_hard": 0.0}]
    if args.combos:
        removals = {name: [f for g in groups for f in FEATURE_GROUPS[g]] for name, groups in COMBOS.items()}
    else:
        removals = dict(FEATURE_GROUPS)
        removals["title+context"] = FEATURE_GROUPS["title"] + FEATURE_GROUPS["context"]  # both encode the title
    for group, fs in removals.items():
        keep = [f for f in features if f not in fs]
        if len(keep) == len(features):
            continue
        r = summary(*cv_scores(pairs, tr, fold, kind, share, keep, args.seed))
        rows.append({"removed": group, "features": len(keep), "mrr5": r[0], "mrr5_nolink": r[1], "mrr5_hard": r[2],
                     "drop": round(base[0] - r[0], 4), "drop_nolink": round(base[1] - r[1], 4),
                     "drop_hard": round(base[2] - r[2], 4)})
        print(f"  without {group:18}: all {r[0]:.4f} ({base[0] - r[0]:+.4f}), no link {r[1]:.4f} "
              f"({base[1] - r[1]:+.4f}), hard no link {r[2]:.4f} ({base[2] - r[2]:+.4f})", flush=True)
    out = args.work / ("ablation_combos_cv.csv" if args.combos else "ablation_cv.csv")
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    if args.combos:
        print(f"-> {out}")
        return 0

    # permutation importance on the held-out folds
    models = {k: fit(kind, pairs, tr[fold != k], features, share, args.seed) for k in range(args.folds)}
    X = pairs.matrix(tr, features, True)  # importance when the entry names no identifier
    y, g = pairs.y[tr], pairs.gidx[tr]

    def oof(Xm):
        s = np.zeros(len(tr))
        for k, m in models.items():
            held = fold == k
            s[held] = score(m, kind, Xm[held])
        return s

    s0 = oof(X)
    ref_all, ref_hard = mrr_at_5(s0, y, g)["mrr5"], mrr_at_5(s0[hard], y[hard], g[hard])["mrr5"]
    rng = np.random.default_rng(args.seed)
    imp = []
    for j, feat in enumerate(features):
        d_all, d_hard = [], []
        for _ in range(args.repeats):
            Xp = X.copy()
            Xp[:, j] = Xp[rng.permutation(len(Xp)), j]
            s = oof(Xp)
            d_all.append(ref_all - mrr_at_5(s, y, g)["mrr5"])
            d_hard.append(ref_hard - mrr_at_5(s[hard], y[hard], g[hard])["mrr5"])
        imp.append({"feature": feat, "group": next(gr for gr, fs in FEATURE_GROUPS.items() if feat in fs),
                    "mrr5_drop": round(float(np.mean(d_all)), 4), "mrr5_drop_hard": round(float(np.mean(d_hard)), 4)})
        print(f"  shuffled {feat:22}: drop all {imp[-1]['mrr5_drop']:+.4f}, hard {imp[-1]['mrr5_drop_hard']:+.4f}", flush=True)
    imp.sort(key=lambda r: (-r["mrr5_drop"], -r["mrr5_drop_hard"]))
    with open(args.work / "importance_cv.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(imp[0]))
        w.writeheader()
        w.writerows(imp)
    print(f"-> {args.work / 'ablation_cv.csv'}, {args.work / 'importance_cv.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
