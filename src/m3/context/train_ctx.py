"""Milestone 3, context experiment - step 4: which reference does an entry cite, from its context only?

    python src/m3/context/train_ctx.py --work WORK [--ctx WORK/context] [--ctx-set compact] [--folds 5]
                                       [--seed 11] [--test]

The rows, labels, split, folds (by publication, same seed as modeling/train.py)
and metric (MRR@5, pessimistic ties) are those of the main model; the features
are CTX/features.parquet (ctx_features.py). Scorers, cross-validated on train:

  random         a random order of the candidates (the floor)
  tfidf_title    cosine TF-IDF of the citing sentences with the candidate title
  tfidf_desc     cosine TF-IDF of the citing sentences with the candidate's
                 description by the other papers (0 when it has none)
  ctx            LightGBM lambdarank on the context features only: no field
                 of the BibTeX entry
  ctx+assign     ctx, then a one-to-one assignment per publication: two entries
                 do not cite the same reference (Hungarian algorithm on the
                 scores of all the entries of the publication, each entry may
                 also take "no candidate" at score THETA, chosen on the
                 out-of-fold scores); the assigned candidate goes first
  meta           the main model's configuration (compact features, link mask
                 share 0.5): metadata only, for comparison
  meta+ctx       compact metadata features + context features

Reported out of fold: all labelled entries, the entries with a context (the
other ~1% are never cited in the text; a context model cannot know them), and
for meta / meta+ctx also with every link feature masked and on the hard
entries (title not identical) with no link, as in modeling/train.py.

--test: the configurations are fitted on the whole train set (THETA from
cross-validation) and scored once on test. --ctx-set: the context features,
ctx_features.CTX_SETS (full: 27, compact: 13, chosen by analyze_ctx.py).
Writes CTX/ctx_cv_report_<set>.json (and CTX/ctx_test_report.json),
CTX/oof_scores_<set>.parquet.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from scipy.optimize import linear_sum_assignment

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "modeling"))
from ctx_features import CTX_FEATURES, CTX_SETS  # noqa: E402
from pairs import COMPACT, Pairs, mrr_at_5  # noqa: E402
from models import score  # noqa: E402
from train import fit, hard_groups, make_folds  # noqa: E402

META_SHARE = 0.5
THETA_QUANTILES = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)


def assign(pairs: Pairs, rows: np.ndarray, s: np.ndarray, theta: float) -> np.ndarray:
    """One-to-one assignment per publication; returns the scores with the assigned candidate moved first."""
    out = s.copy()
    papers = pairs.arxiv_id[rows]
    order = np.argsort(papers, kind="stable")
    bounds = np.r_[0, np.flatnonzero(papers[order][1:] != papers[order][:-1]) + 1, len(order)]
    boost = (np.nanmax(np.abs(s)) + 1) * 2 if len(s) else 1
    for b0, b1 in zip(bounds[:-1], bounds[1:]):
        idx = order[b0:b1]
        g, g_inv = np.unique(pairs.gidx[rows[idx]], return_inverse=True)
        c, c_inv = np.unique(pairs.ref[rows[idx]].astype(str), return_inverse=True)
        M = np.full((len(g), len(c)), -1e9)
        M[g_inv, c_inv] = s[idx]
        full = np.hstack([M, np.full((len(g), len(g)), theta)])  # "no candidate" columns
        r, k = linear_sum_assignment(full, maximize=True)
        chosen = {(gi, ci) for gi, ci in zip(r, k) if ci < len(c)}
        hit = np.array([(gi, ci) in chosen for gi, ci in zip(g_inv, c_inv)])
        out[idx[hit]] += boost
    return out


def metrics(pairs: Pairs, rows: np.ndarray, s: np.ndarray, extra: dict[str, np.ndarray]) -> dict:
    """MRR@5 on the sample rows of `rows` (s aligned with rows), on subsets given by boolean masks."""
    samp = pairs.is_sample[rows]
    out = {"all": mrr_at_5(s[samp], pairs.y[rows][samp], pairs.gidx[rows][samp])}
    for name, m in extra.items():
        k = samp & m
        out[name] = mrr_at_5(s[k], pairs.y[rows][k], pairs.gidx[rows][k])
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--ctx", type=Path)
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--ctx-set", choices=sorted(CTX_SETS), default="compact")
    p.add_argument("--test", action="store_true", help="also fit on train and score test once")
    args = p.parse_args(argv)
    ctx = args.ctx or args.work / "context"
    t0 = time.perf_counter()
    from features import FEATURES  # noqa: E402 (src/m3, on the path through pairs)
    pairs = Pairs(ctx, columns=list(dict.fromkeys(FEATURES + CTX_FEATURES)))
    col = {c: i for i, c in enumerate(pairs.columns)}
    cf = CTX_SETS[args.ctx_set]
    feats = {"ctx": cf, "meta": COMPACT, "meta+ctx": COMPACT + cf}
    share = {"ctx": 0.0, "meta": META_SHARE, "meta+ctx": META_SHARE}

    tr_s = pairs.rows("train")  # labelled entries (samples) of train: what the models learn from
    tr = pairs.rows("train", samples=False)  # every entry of train: what the assignment sees
    fold_s = make_folds(pairs, tr_s, args.folds, args.seed)
    fold_of = dict(zip(pairs.arxiv_id[tr_s], fold_s))
    fold = np.array([fold_of.get(a, -1) for a in pairs.arxiv_id[tr]])
    has_ctx = pairs.X[:, col["ctx_missing"]] == 0
    hard_s = hard_groups(pairs, tr_s)
    hard = np.zeros(len(pairs.y), bool)
    hard[tr_s[hard_s]] = True
    print(f"train: {len(tr_s)} sample pairs, {len(np.unique(pairs.gidx[tr_s]))} labelled entries "
          f"({len(np.unique(pairs.gidx[tr_s][has_ctx[tr_s]]))} with a context); {len(tr)} pairs with the unlabelled "
          f"entries", flush=True)

    rng = np.random.default_rng(args.seed)
    S = {"random": rng.random(len(pairs.y)),
         "tfidf_title": np.nan_to_num(pairs.X[:, col["sim_sent_title"]]),
         "tfidf_desc": np.nan_to_num(pairs.X[:, col["sim_sent_desc"]])}
    S_nl = {}
    for name in ("ctx", "meta", "meta+ctx"):
        s, s_nl = np.zeros(len(pairs.y)), np.zeros(len(pairs.y))
        t = time.perf_counter()
        for k in range(args.folds):
            model = fit("lgbm", pairs, tr_s[fold_s != k], feats[name], share[name], args.seed)
            held = tr[fold == k]
            s[held] = score(model, "lgbm", pairs.matrix(held, feats[name]))
            s_nl[held] = score(model, "lgbm", pairs.matrix(held, feats[name], True))
        S[name], S_nl[name] = s, s_nl
        print(f"  {name}: {time.perf_counter() - t:.0f} s", flush=True)

    # one-to-one assignment: theta among quantiles of the out-of-fold scores of train
    best = None
    for q in THETA_QUANTILES:
        theta = float(np.quantile(S["ctx"][tr], q))
        r = metrics(pairs, tr, assign(pairs, tr, S["ctx"][tr], theta), {"with_context": has_ctx[tr]})
        print(f"  assignment theta q{q:.1f} = {theta:+.3f}: MRR@5 {r['with_context']['mrr5']:.4f}", flush=True)
        if best is None or r["with_context"]["mrr5"] > best[0]:
            best = (r["with_context"]["mrr5"], q, theta)
    theta_q = best[1]
    S["ctx+assign"] = np.zeros(len(pairs.y))
    S["ctx+assign"][tr] = assign(pairs, tr, S["ctx"][tr], best[2])
    for name in ("meta+ctx",):
        S_nl[name + "+assign"] = np.zeros(len(pairs.y))
        S_nl[name + "+assign"][tr] = assign(pairs, tr, S_nl[name][tr], float(np.quantile(S_nl[name][tr], theta_q)))

    report = {"ctx_set": args.ctx_set, "folds": args.folds, "theta_quantile": theta_q, "features": feats, "cv": {}}
    subsets = {"with_context": has_ctx[tr], "hard": hard[tr]}
    for name, s in S.items():
        report["cv"][name] = metrics(pairs, tr, s[tr], subsets)
    for name, s in S_nl.items():
        report["cv"][name + " (no link)"] = metrics(pairs, tr, s[tr], subsets)
    (ctx / f"ctx_cv_report_{args.ctx_set}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    pq.write_table(pa.table({"row": tr, "arxiv_id": pairs.arxiv_id[tr].astype(str),
                             "group": pairs.group[tr].astype(str), "ref_arxiv_id": pairs.ref[tr].astype(str),
                             "y": pairs.y[tr], "is_sample": pairs.is_sample[tr], "fold": fold,
                             **{f"s_{n.replace('+', '_')}": S[n][tr] for n in S},
                             **{f"s_{n.replace('+', '_')}_nolink": S_nl[n][tr] for n in S_nl}}),
                   ctx / f"oof_scores_{args.ctx_set}.parquet", compression="zstd")

    print(f"\nout-of-fold MRR@5 on train ({args.folds} folds by publication); theta quantile {theta_q}")
    print(f"{'':26}{'all':>8}{'context':>9}{'hard':>8}{'hit@1 ctx':>11}")
    for name, r in report["cv"].items():
        print(f"{name:26}{r['all']['mrr5']:8.4f}{r['with_context']['mrr5']:9.4f}{r['hard']['mrr5']:8.4f}"
              f"{r['with_context']['hit1']:11.4f}")

    if args.test:
        te = pairs.rows("test", samples=False)
        has_te, hard_te = has_ctx[te], np.zeros(len(te), bool)
        te_s = pairs.rows("test")
        hard_all = np.zeros(len(pairs.y), bool)
        hard_all[te_s[hard_groups(pairs, te_s)]] = True
        hard_te = hard_all[te]
        T, T_nl = {"random": S["random"][te], "tfidf_title": S["tfidf_title"][te], "tfidf_desc": S["tfidf_desc"][te]}, {}
        for name in ("ctx", "meta", "meta+ctx"):
            model = fit("lgbm", pairs, tr_s, feats[name], share[name], args.seed)
            T[name] = score(model, "lgbm", pairs.matrix(te, feats[name]))
            T_nl[name] = score(model, "lgbm", pairs.matrix(te, feats[name], True))
        T["ctx+assign"] = assign(pairs, te, T["ctx"], float(np.quantile(S["ctx"][tr], theta_q)))
        T_nl["meta+ctx+assign"] = assign(pairs, te, T_nl["meta+ctx"],
                                         float(np.quantile(S_nl["meta+ctx"][tr], theta_q)))
        test = {}
        sub = {"with_context": has_te, "hard": hard_te}
        for name, s in T.items():
            test[name] = metrics(pairs, te, s, sub)
        for name, s in T_nl.items():
            test[name + " (no link)"] = metrics(pairs, te, s, sub)
        (ctx / "ctx_test_report.json").write_text(json.dumps({"ctx_set": args.ctx_set, **test}, indent=1), encoding="utf-8")
        print("\ntest (scored once)")
        for name, r in test.items():
            print(f"{name:26}{r['all']['mrr5']:8.4f}{r['with_context']['mrr5']:9.4f}{r['hard']['mrr5']:8.4f}"
                  f"{r['with_context']['hit1']:11.4f}")
    print(f"-> {ctx / f'ctx_cv_report_{args.ctx_set}.json'} ({time.perf_counter() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
