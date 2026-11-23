"""Milestone 3, modelling - error analysis: where the chosen model (and the baselines) rank wrongly.

    python src/m3/modeling/errors.py --work WORK [--folds 5]

Two sources of errors:
  test      the final model on the test set (the part already evaluated once by
            evaluate.py --part test; nothing is chosen from it any more)
  train_cv  out-of-fold scores of the chosen configuration on train (the folds
            of train.py), many more entries, the hard cases included
each as is and with every link feature masked ("no link").

For every labelled entry whose reference is not ranked first by the chosen
model, or by the title baseline, one row: the entry (title, authors, year),
the labelled reference and the reference ranked first, their title
similarity / first author / year gap, the rank of the labelled reference for
the model and for the title baseline, and a rough category:
  same title, other work   the first candidate has a title as close as the
                           answer's (>= 0.8) but other authors or years
  rewritten title          the answer's title is far from the entry's (< 0.5):
                           renamed paper, translated or abridged title
  missing information      the entry has no title or no authors
  close candidates         the answer and the first candidate are both close
  other
Also counts how many entries the model ranks right where the title baseline
does not ("model fixes") and the reverse ("model breaks").

Then the matches the automatic rules probably missed: entries without label
(is_match = false) of the automatic publications whose best candidate for the
chosen model looks like the same work (title similarity >= 0.5 and the same
first author); a hidden source of easy-looking data (those hard pairs never
became samples). Counted at three strictness levels, listed in
WORK/missed_matches.csv.

Writes WORK/errors.csv, WORK/errors_summary.json and WORK/missed_matches.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Pairs  # noqa: E402
from models import baseline_scores, load, score  # noqa: E402
from train import cv_scores, make_folds  # noqa: E402


def ranks(s: np.ndarray, y: np.ndarray, g: np.ndarray) -> dict[int, tuple[int, int]]:
    """group -> (rank of the gold row, index of the first row) within the given rows (pessimistic ties)."""
    out = {}
    order = np.lexsort((-s, g))
    first_of = {}
    for i in order:
        first_of.setdefault(g[i], i)
    gold = np.flatnonzero(y == 1)
    gold_score = {g[i]: s[i] for i in gold}
    before = Counter(g[i] for i in range(len(s)) if y[i] == 0 and s[i] >= gold_score.get(g[i], np.inf))
    for i in gold:
        out[g[i]] = (before[g[i]] + 1, first_of[g[i]], i)
    return out


def category(pairs: Pairs, gold: int, first: int) -> str:
    c = pairs.columns
    v = lambda i, f: pairs.X[i, c.index(f)]  # noqa: E731
    if v(gold, "entry_title_missing") == 1 or v(gold, "entry_authors_missing") == 1:
        return "missing information"
    if v(gold, "title_jacc3") < 0.5:
        return "rewritten title"
    if v(first, "title_jacc3") >= 0.8 and (v(first, "first_author_match") != 1 or v(first, "year_gap") > 1):
        return "same title, other work"
    if v(first, "title_jacc3") >= 0.5:
        return "close candidates"
    return "other"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--seed", type=int, default=11)
    args = p.parse_args(argv)
    folder = args.work / "models"
    chosen = json.loads((folder / "chosen.json").read_text(encoding="utf-8"))["model"]
    model, meta = load(folder, chosen)
    feats, kind, share = meta["features"], meta["kind"], meta["mask_share"]
    pairs = Pairs(args.work)
    entries = {(e["arxiv_id"], e["citation_key"]): e for e in pq.read_table(
        args.work / "entries_clean.parquet", columns=["arxiv_id", "citation_key", "title", "authors", "year"]).to_pylist()}
    refs = {(r["arxiv_id"], r["ref_arxiv_id"]): r for r in pq.read_table(
        args.work / "references_clean.parquet", columns=["arxiv_id", "ref_arxiv_id", "ref_title", "ref_authors", "year"]).to_pylist()}

    sources = {}
    te = pairs.rows("test")
    sources["test"] = (te, score(model, kind, pairs.matrix(te, feats)), score(model, kind, pairs.matrix(te, feats, True)))
    tr = pairs.rows("train")
    print(f"out-of-fold scores of {chosen} on train ({args.folds} folds)...", flush=True)
    s_tr, s_tr_nl = cv_scores(pairs, tr, make_folds(pairs, tr, args.folds, args.seed), kind, share, feats, args.seed)
    sources["train_cv"] = (tr, s_tr, s_tr_nl)

    rows, summary = [], {}
    for src, (idx, s, s_nl) in sources.items():
        y, g = pairs.y[idx], pairs.gidx[idx]
        base = baseline_scores(pairs, idx, "title")
        rb = ranks(base, y, g)
        for setting, sc in (("as is", s), ("no link", s_nl)):
            rm = ranks(sc, y, g)
            cats = Counter()
            fixes = sum(1 for k in rm if rm[k][0] == 1 and rb[k][0] > 1)
            breaks = sum(1 for k in rm if rm[k][0] > 1 and rb[k][0] == 1)
            for k, (rank, first, gold) in rm.items():
                if rank == 1 and rb[k][0] == 1:
                    continue
                gi, fi = idx[gold], idx[first]
                cat = category(pairs, gi, fi) if rank > 1 else "(baseline only)"
                if rank > 1:
                    cats[cat] += 1
                pid, key = pairs.arxiv_id[gi], pairs.citation_key[gi]
                e, gr, fr = entries[(pid, key)], refs[(pid, pairs.ref[gi])], refs[(pid, pairs.ref[fi])]
                c = pairs.columns
                rows.append({"source": src, "setting": setting, "arxiv_id": pid, "citation_key": key,
                             "model_rank": rank, "title_baseline_rank": rb[k][0], "category": cat,
                             "entry_title": e["title"], "entry_first_author": (e["authors"] or [""])[0], "entry_year": e["year"],
                             "gold_ref": pairs.ref[gi], "gold_title": gr["ref_title"],
                             "gold_first_author": (gr["ref_authors"] or [""])[0], "gold_year": gr["year"],
                             "gold_title_sim": round(float(pairs.X[gi, c.index("title_jacc3")]), 3),
                             "first_ref": pairs.ref[fi], "first_title": fr["ref_title"],
                             "first_first_author": (fr["ref_authors"] or [""])[0], "first_year": fr["year"],
                             "first_title_sim": round(float(pairs.X[fi, c.index("title_jacc3")]), 3)})
            n = len(rm)
            wrong = sum(1 for v in rm.values() if v[0] > 1)
            summary[f"{src} / {setting}"] = {"entries": n, "model_not_first": wrong, "title_baseline_not_first":
                                             sum(1 for v in rb.values() if v[0] > 1), "model_fixes": fixes,
                                             "model_breaks": breaks, "categories": dict(cats.most_common())}
            print(f"{src:9} {setting:8}: {n} entries, model wrong at rank 1: {wrong}, title baseline: "
                  f"{summary[f'{src} / {setting}']['title_baseline_not_first']}, model fixes {fixes}, breaks {breaks}; "
                  f"{dict(cats.most_common())}", flush=True)
    # entries without label whose best candidate looks like the same work
    nos = np.flatnonzero(~pairs.is_sample)
    s_all = score(model, kind, pairs.matrix(nos, feats))
    best = {}
    for i, sc in zip(nos, s_all):
        if pairs.gidx[i] not in best or sc > best[pairs.gidx[i]][1]:
            best[pairs.gidx[i]] = (i, sc)
    c = pairs.columns
    v = lambda i, f: float(pairs.X[i, c.index(f)])  # noqa: E731
    levels = {"title >= 0.8 + first author": lambda i: v(i, "title_jacc3") >= 0.8 and v(i, "first_author_match") == 1,
              "title >= 0.6 + authors >= 0.3": lambda i: v(i, "title_jacc3") >= 0.6 and v(i, "author_jacc") >= 0.3,
              "title >= 0.5 + first author + years <= 2": lambda i: v(i, "title_jacc3") >= 0.5
              and v(i, "first_author_match") == 1 and v(i, "year_gap") <= 2}
    missed = {name: dict(Counter(pairs.source[i] for i, _ in best.values() if f(i))) for name, f in levels.items()}
    summary["possibly missed matches"] = {"entries without label": dict(Counter(pairs.source[i] for i, _ in best.values())),
                                          **missed}
    print("entries without label whose best candidate looks like the same work:", missed, flush=True)
    loose = levels["title >= 0.5 + first author + years <= 2"]
    with open(args.work / "missed_matches.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["arxiv_id", "citation_key", "source", "entry_title", "entry_year", "candidate", "candidate_title",
                    "candidate_year", "title_sim", "year_gap"])
        for i, _ in best.values():
            if loose(i):
                pid, key = pairs.arxiv_id[i], pairs.citation_key[i]
                e, r = entries[(pid, key)], refs[(pid, pairs.ref[i])]
                w.writerow([pid, key, pairs.source[i], e["title"], e["year"], pairs.ref[i], r["ref_title"], r["year"],
                            round(v(i, "title_jacc3"), 3), v(i, "year_gap")])

    with open(args.work / "errors.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (args.work / "errors_summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"-> {args.work / 'errors.csv'} ({len(rows)} rows), {args.work / 'errors_summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
