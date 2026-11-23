"""Milestone 3, modelling - choose the model by cross-validation on train, fit it on train.

    python src/m3/modeling/train.py --work WORK [--seed 11] [--folds 5] [--mask-shares 0 0.5]

Why cross-validation: the valid set (10 publications, 400 labelled entries) is
too easy to tell scorers apart - every scorer, the title baseline included,
reaches MRR@5 = 1 on it. The train set has 62 866 labelled entries; it is cut
by publication into --folds folds, and every candidate configuration is
trained on the other folds and scored on the held-out one (out-of-fold
scores). The test set is never read here; valid is only reported.

Configurations (each with every link-mask share of --mask-shares: the share of
the training entries whose link features are set to "no arXiv id, no DOI",
common.NO_LINK, so that the model also learns from title, authors, year):
  logreg_m<share>   logistic regression on the pairs (pairwise classification)
  lgbm_m<share>     LightGBM lambdarank, one group per entry (learning to rank),
                    300 trees, learning rate 0.05 (no early stopping: it would
                    look at the held-out fold)
and the baselines rules / title for comparison.

Out-of-fold MRR@5 is reported on: all, manual, automatic publications; with
every link feature masked ("no link"); and on the hard entries, whose
labelled reference does not have an identical title (title_exact = 0), where
the title alone does not settle the answer. The chosen configuration has the
best mean of out-of-fold MRR@5 on all, no link and hard with no link (most
training labels come from the rules, so "all" and "hard" as is are near 1 for
every scorer that sees the link features; the no-link scores measure matching
without identifier); it is then fitted on the whole train set.

Writes WORK/models/<name>.pkl + .json (final models), WORK/models/chosen.json,
WORK/train_report.json (cross-validation and valid metrics).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import FEATURE_SETS, Pairs, link_mask_for_groups, mrr_at_5  # noqa: E402
from models import BASELINES, LGBM_PARAMS, baseline_scores, fit_lgbm, fit_logreg, save, score  # noqa: E402

def evaluate_scorer(pairs: Pairs, part: str, scorer) -> dict:
    """scorer(rows, mask_link) -> scores; metrics on all / manual / automatic, as is and with no link."""
    out = {}
    for source in (None, "manual", "auto"):
        rows = pairs.rows(part, source)
        if len(rows) == 0:
            continue
        for masked in (False, True):
            key = f"{source or 'all'}{'_nolink' if masked else ''}"
            out[key] = mrr_at_5(scorer(rows, masked), pairs.y[rows], pairs.gidx[rows])
    return out


def hard_groups(pairs: Pairs, rows: np.ndarray) -> np.ndarray:
    """Boolean per row of `rows`: the entry's labelled reference does not have an identical title."""
    j = pairs.columns.index("title_exact")
    gold = rows[pairs.y[rows] == 1]
    hard = np.zeros(pairs.gidx.max() + 1, bool)
    hard[pairs.gidx[gold]] = ~(pairs.X[gold, j] == 1)
    return hard[pairs.gidx[rows]]


def oof_metrics(pairs: Pairs, rows: np.ndarray, s: np.ndarray, s_nolink: np.ndarray, hard: np.ndarray) -> dict:
    y, g, src = pairs.y[rows], pairs.gidx[rows], pairs.source[rows]
    out = {"all": mrr_at_5(s, y, g), "all_nolink": mrr_at_5(s_nolink, y, g), "hard": mrr_at_5(s[hard], y[hard], g[hard]),
           "hard_nolink": mrr_at_5(s_nolink[hard], y[hard], g[hard])}
    for source in ("manual", "auto"):
        m = src == source
        out[source] = mrr_at_5(s[m], y[m], g[m])
        out[f"{source}_nolink"] = mrr_at_5(s_nolink[m], y[m], g[m])
    return out


def fit(kind: str, pairs: Pairs, rows: np.ndarray, features: list[str], share: float, seed: int):
    mask = link_mask_for_groups(pairs.gidx[rows], share, seed) if share > 0 else False
    X, y = pairs.matrix(rows, features, mask), pairs.y[rows]
    if kind == "logreg":
        return fit_logreg(X, y, seed)
    return fit_lgbm(X, y, pairs.group_sizes(rows), seed)


def make_folds(pairs: Pairs, rows: np.ndarray, folds: int, seed: int) -> np.ndarray:
    """Fold number per row, by publication (a publication is never split between folds)."""
    papers = np.unique(pairs.arxiv_id[rows])
    fold_of = dict(zip(np.random.default_rng(seed).permutation(papers), np.arange(len(papers)) % folds))
    return np.asarray([fold_of[p] for p in pairs.arxiv_id[rows]])


def cv_scores(pairs: Pairs, rows: np.ndarray, fold: np.ndarray, kind: str, share: float, features: list[str],
              seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-fold scores of every row, as is and with no link."""
    s, s_nl = np.zeros(len(rows)), np.zeros(len(rows))
    for k in np.unique(fold):
        held = np.flatnonzero(fold == k)
        model = fit(kind, pairs, rows[fold != k], features, share, seed)
        s[held] = score(model, kind, pairs.matrix(rows[held], features))
        s_nl[held] = score(model, kind, pairs.matrix(rows[held], features, True))
    return s, s_nl


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--mask-shares", type=float, nargs="+", default=[0.0, 0.5])
    p.add_argument("--feature-set", choices=sorted(FEATURE_SETS), default="compact",
                   help="common.FEATURE_SETS: full (26 features) or compact (17, chosen by ablation.py)")
    p.add_argument("--report", type=Path, help="default WORK/train_report.json")
    args = p.parse_args(argv)
    for f in ("features.parquet", "splits.json"):
        if not (args.work / f).exists():  # pipeline_m3 runs every step present
            print(f"skipped: no {args.work / f}")
            return 0
    t0 = time.perf_counter()
    pairs = Pairs(args.work)
    features = FEATURE_SETS[args.feature_set]
    tr = pairs.rows("train")
    papers = np.unique(pairs.arxiv_id[tr])
    fold = make_folds(pairs, tr, args.folds, args.seed)
    hard = hard_groups(pairs, tr)
    print(f"{len(pairs.y)} pairs; train {len(tr)} pairs, {len(np.unique(pairs.gidx[tr]))} entries "
          f"({len(np.unique(pairs.gidx[tr][hard]))} hard), {len(papers)} publications in {args.folds} folds; "
          f"{len(features)} features", flush=True)

    report = {"feature_set": args.feature_set, "features": features, "folds": args.folds, "cv": {}, "valid": {}}
    for name in BASELINES:
        report["cv"][name] = oof_metrics(pairs, tr, baseline_scores(pairs, tr, name),
                                         baseline_scores(pairs, tr, name, True), hard)
    configs = [(kind, share) for share in args.mask_shares for kind in ("logreg", "lgbm")]
    for kind, share in configs:
        name = f"{kind}_m{int(round(share * 100)):02d}"
        t = time.perf_counter()
        s, s_nl = cv_scores(pairs, tr, fold, kind, share, features, args.seed)
        report["cv"][name] = oof_metrics(pairs, tr, s, s_nl, hard)
        c = report["cv"][name]
        print(f"  {name}: {time.perf_counter() - t:.0f} s; out-of-fold MRR@5 all {c['all']['mrr5']:.4f}, "
              f"no link {c['all_nolink']['mrr5']:.4f}, hard {c['hard']['mrr5']:.4f}", flush=True)

    learned = [f"{k}_m{int(round(s * 100)):02d}" for k, s in configs]
    # the honest criteria: on train most labels come from the rules, so "all" and "hard" as is are nearly 1 for any
    # scorer that sees the link features; "no link" (and "hard" with no link) measure matching without identifier
    key = lambda n: np.mean([report["cv"][n][k]["mrr5"] for k in ("all", "all_nolink", "hard_nolink")])  # noqa: E731
    chosen = max(learned, key=key)
    report["chosen"] = chosen
    for name in learned:  # final models on the whole train set
        kind, share = name.split("_m")[0], int(name.split("_m")[1]) / 100
        model = fit(kind, pairs, tr, features, share, args.seed)
        save(model, kind, features, {"mask_share": share, **({"params": LGBM_PARAMS} if kind == "lgbm" else {})},
             args.work / "models", name)
        report["valid"][name] = evaluate_scorer(
            pairs, "valid", lambda rows, m, mo=model, ki=kind: score(mo, ki, pairs.matrix(rows, features, m)))
    for name in BASELINES:
        report["valid"][name] = evaluate_scorer(pairs, "valid", lambda rows, m, n=name: baseline_scores(pairs, rows, n, m))
    (args.work / "models" / "chosen.json").write_text(json.dumps({"model": chosen}, indent=1), encoding="utf-8")
    report_path = args.report or args.work / "train_report.json"
    report_path.write_text(json.dumps(report, indent=1), encoding="utf-8")

    print(f"\nout-of-fold MRR@5 on train ({args.folds}-fold by publication)")
    print(f"{'':12}{'all':>8}{'manual':>8}{'auto':>8}{'hard':>8} | no link:{'all':>7}{'manual':>8}{'hard':>8}")
    for n in list(BASELINES) + learned:
        c = report["cv"][n]
        print(f"{n:12}{c['all']['mrr5']:8.4f}{c['manual']['mrr5']:8.4f}{c['auto']['mrr5']:8.4f}{c['hard']['mrr5']:8.4f} |"
              f"{c['all_nolink']['mrr5']:15.4f}{c['manual_nolink']['mrr5']:8.4f}{c['hard_nolink']['mrr5']:8.4f}"
              f"{'   <- chosen' if n == chosen else ''}")
    print(f"valid (sanity): " + ", ".join(f"{n} {report['valid'][n]['all']['mrr5']:.4f}" for n in report["valid"]))
    print(f"-> {report_path}, models in {args.work / 'models'} ({time.perf_counter() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
