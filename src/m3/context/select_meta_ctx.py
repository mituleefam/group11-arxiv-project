"""Milestone 3, context experiment - step 6: feature selection for the combined model, and its gain over meta.

    python src/m3/context/select_meta_ctx.py --work WORK [--ctx WORK/context] [--folds 5] [--seed 11]

The 17 metadata features (pairs.COMPACT, the main model's choice) stay fixed;
what is chosen is how many context features to add, by cross-validation on
train only (same folds as train.py / train_ctx.py):

  meta           17 metadata features, the main model's configuration
  meta+ctx4      + the 4 context features with the largest permutation
                 importance (analyze_ctx.py): sim_local_title, sim_local_desc,
                 n_desc, fa_in_local
  meta+ctx13     + ctx_features.CTX_COMPACT
  meta+ctx27     + every context feature

each also with the one-to-one assignment (train_ctx.assign, THETA at the 0.1
quantile of the out-of-fold scores, the value train_ctx.py chose). LightGBM,
link mask share 0.5 as the main model. Key, as in modeling/train.py: the mean
of MRR@5 on all, all with no link and hard with no link (with the
assignment); the chosen set is the smallest within TOLERANCE of the best key.

Then meta and the chosen set are fitted on the whole train set and scored on
valid and on test, as is and with no link, all and hard entries, so that the
gain over meta is read on every part. Writes CTX/select_meta_ctx.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "modeling"))
from ctx_features import CTX_COMPACT, CTX_FEATURES  # noqa: E402
from train_ctx import assign  # noqa: E402
from pairs import COMPACT, Pairs, mrr_at_5  # noqa: E402
from models import score  # noqa: E402
from train import fit, hard_groups, make_folds  # noqa: E402

TOP4 = ["sim_local_title", "sim_local_desc", "n_desc", "fa_in_local"]
OPTIONS = {"meta": COMPACT, "meta+ctx4": COMPACT + TOP4, "meta+ctx13": COMPACT + CTX_COMPACT,
           "meta+ctx27": COMPACT + CTX_FEATURES}
SHARE, THETA_Q, TOLERANCE = 0.5, 0.1, 0.0005


def evaluate(pairs: Pairs, rows: np.ndarray, hard: np.ndarray, s: np.ndarray, s_nl: np.ndarray,
             theta: float, theta_nl: float) -> dict:
    """MRR@5 (and hit@1) on all / hard, as is / no link, without / with the assignment."""
    samp = pairs.is_sample[rows]
    y, g = pairs.y[rows], pairs.gidx[rows]
    out = {}
    for tag, sc, th in (("", s, theta), ("_nolink", s_nl, theta_nl)):
        for a_tag, x in (("", sc), ("+assign", assign(pairs, rows, sc, th))):
            for sub, m in (("all", samp), ("hard", samp & hard)):
                out[f"{sub}{tag}{a_tag}"] = mrr_at_5(x[m], y[m], g[m])
    return out


def key(r: dict) -> float:
    return float(np.mean([r[k]["mrr5"] for k in ("all+assign", "all_nolink+assign", "hard_nolink+assign")]))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--ctx", type=Path)
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--seed", type=int, default=11)
    args = p.parse_args(argv)
    ctx = args.ctx or args.work / "context"
    t0 = time.perf_counter()
    from features import FEATURES  # noqa: E402
    pairs = Pairs(ctx, columns=list(dict.fromkeys(FEATURES + CTX_FEATURES)))

    def hard_of(part):
        s_rows = pairs.rows(part)
        h = np.zeros(len(pairs.y), bool)
        h[s_rows[hard_groups(pairs, s_rows)]] = True
        return h

    tr_s, tr = pairs.rows("train"), pairs.rows("train", samples=False)
    fold_s = make_folds(pairs, tr_s, args.folds, args.seed)
    fold_of = dict(zip(pairs.arxiv_id[tr_s], fold_s))
    fold = np.array([fold_of.get(a, -1) for a in pairs.arxiv_id[tr]])
    hard_tr = hard_of("train")[tr]

    report = {"options": {k: len(v) for k, v in OPTIONS.items()}, "top4": TOP4, "theta_quantile": THETA_Q,
              "cv": {}, "theta": {}}
    for name, feats in OPTIONS.items():
        s, s_nl = np.zeros(len(tr)), np.zeros(len(tr))
        for k in range(args.folds):
            m = fit("lgbm", pairs, tr_s[fold_s != k], feats, SHARE, args.seed)
            held = fold == k
            s[held] = score(m, "lgbm", pairs.matrix(tr[held], feats))
            s_nl[held] = score(m, "lgbm", pairs.matrix(tr[held], feats, True))
        th, th_nl = float(np.quantile(s, THETA_Q)), float(np.quantile(s_nl, THETA_Q))
        report["theta"][name] = [th, th_nl]
        report["cv"][name] = evaluate(pairs, tr, hard_tr, s, s_nl, th, th_nl)
        r = report["cv"][name]
        print(f"{name:11} ({len(feats)}): key {key(r):.5f}; hard no link {r['hard_nolink']['mrr5']:.4f}, "
              f"+assign {r['hard_nolink+assign']['mrr5']:.4f} ({time.perf_counter() - t0:.0f} s)", flush=True)
    best = max(key(r) for r in report["cv"].values())
    chosen = min((n for n in OPTIONS if n != "meta" and key(report["cv"][n]) >= best - TOLERANCE),
                 key=lambda n: len(OPTIONS[n]))
    report["chosen"] = chosen
    print(f"chosen: {chosen} ({len(OPTIONS[chosen])} features)", flush=True)

    # meta and the chosen set on valid and test
    for part in ("valid", "test"):
        rows, h = pairs.rows(part, samples=False), hard_of(part)
        report[part] = {}
        for name in ("meta", chosen):
            m = fit("lgbm", pairs, tr_s, OPTIONS[name], SHARE, args.seed)
            s = score(m, "lgbm", pairs.matrix(rows, OPTIONS[name]))
            s_nl = score(m, "lgbm", pairs.matrix(rows, OPTIONS[name], True))
            report[part][name] = evaluate(pairs, rows, h[rows], s, s_nl, *report["theta"][name])
        print(f"{part}: " + "; ".join(f"{n} hard no link {r['hard_nolink+assign']['mrr5']:.4f}"
                                      for n, r in report[part].items()), flush=True)
    (ctx / "select_meta_ctx.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"-> {ctx / 'select_meta_ctx.json'} ({time.perf_counter() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
