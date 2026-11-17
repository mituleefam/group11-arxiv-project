"""Milestone 3 - how much each feature says on its own (to justify and to select features).

    python src/m3/feature_report.py --work WORK [--splits WORK/splits.json] [--part train]
                                    [--out WORK/feature_report.csv]

On the samples (is_sample = 1) of one part of the split (default: train, so
that the choice of features never looks at the test set; without splits.json
every sample is used), for every feature of features.parquet:
  missing        share of pairs where the feature is unknown
  mean_yes       mean on the labelled pairs (y = 1)
  mean_no        mean on the other candidates of the same entries (y = 0)
  mrr5_alone     MRR@5 when the candidates of each entry are ranked by this
                 feature alone (higher first, or lower first for the features
                 where smaller is better: gaps and ranks; ties broken against
                 the labelled pair, so a constant feature scores low)
  mrr5_manual / mrr5_auto   the same on the manual / automatic publications
and the absolute correlation between features (pairs over 0.9 are listed).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import FEATURE_GROUPS, FEATURES  # noqa: E402

LOWER_IS_BETTER = {"year_gap", "title_rank", "tfidf_rank", "author_rank", "title_gap_best", "n_candidates",
                   "entry_title_missing", "entry_authors_missing", "year_missing"}


def mrr_at_5(groups: dict[str, list[tuple[float, int]]]) -> float:
    total = 0.0
    for pairs in groups.values():
        # labelled pair loses ties: rank = 1 + candidates strictly better + candidates tied
        gold = next((v for v, y in pairs if y == 1), None)
        if gold is None:
            continue
        better = sum(1 for v, y in pairs if y == 0 and v >= gold)
        rank = better + 1
        total += 1.0 / rank if rank <= 5 else 0.0
    n = sum(1 for p in groups.values() if any(y == 1 for _, y in p))
    return total / n if n else float("nan")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--splits", type=Path, help="default WORK/splits.json if it exists")
    p.add_argument("--part", default="train", choices=("train", "valid", "test", "all"))
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    splits = args.splits or args.work / "splits.json"
    keep = None
    if args.part != "all" and splits.exists():
        keep = set(json.loads(splits.read_text(encoding="utf-8"))[args.part])
    elif args.part != "all":
        print(f"no {splits}: every sample is used (the choice of features must later be checked on train only)")
    t = pq.read_table(args.work / "features.parquet", filters=[("is_sample", "=", 1)]).to_pylist()
    if keep is not None:
        t = [r for r in t if r["arxiv_id"] in keep]
    print(f"{len(t)} pairs, {len({r['group'] for r in t})} entries ({args.part})")
    rows = []
    for f in FEATURES:
        vals = [r[f] for r in t]
        known = [(v, r) for v, r in zip(vals, t) if v is not None and not math.isnan(v)]
        yes = [v for v, r in known if r["y"] == 1]
        no = [v for v, r in known if r["y"] == 0]
        sign = -1.0 if f in LOWER_IS_BETTER else 1.0
        out = {"feature": f, "group": next(g for g, fs in FEATURE_GROUPS.items() if f in fs),
               "missing": round(1 - len(known) / max(1, len(vals)), 3),
               "mean_yes": round(sum(yes) / len(yes), 3) if yes else "", "mean_no": round(sum(no) / len(no), 3) if no else ""}
        for name, src in (("mrr5_alone", None), ("mrr5_manual", "manual"), ("mrr5_auto", "auto")):
            groups = defaultdict(list)
            for r in t:
                if src is None or r["source"] == src:
                    v = r[f]
                    v = -math.inf if v is None or math.isnan(v) else sign * v
                    groups[r["group"]].append((v, r["y"]))
            out[name] = round(mrr_at_5(groups), 3) if groups else ""
        rows.append(out)
        print(f"  {f:22} {out['group']:8} missing {out['missing']:5}  yes {out['mean_yes']!s:7} no {out['mean_no']!s:7}"
              f"  MRR@5 alone {out['mrr5_alone']} (manual {out['mrr5_manual']}, auto {out['mrr5_auto']})")
    # correlations (pairs where both known), on a fixed random sample of 100 000 pairs
    import random
    sample = random.Random(0).sample(t, min(100_000, len(t)))
    print("highly correlated pairs (|r| > 0.9):")
    for i, a in enumerate(FEATURES):
        for b in FEATURES[i + 1:]:
            xs = [(r[a], r[b]) for r in sample if r[a] is not None and r[b] is not None
                  and not math.isnan(r[a]) and not math.isnan(r[b])]
            if len(xs) < 100:
                continue
            ma = sum(x for x, _ in xs) / len(xs)
            mb = sum(y for _, y in xs) / len(xs)
            va = sum((x - ma) ** 2 for x, _ in xs)
            vb = sum((y - mb) ** 2 for _, y in xs)
            if va and vb:
                r = sum((x - ma) * (y - mb) for x, y in xs) / math.sqrt(va * vb)
                if abs(r) > 0.9:
                    print(f"  {a} ~ {b}: {r:.2f}")
    out_path = args.out or args.work / f"feature_report_{args.part}.csv"
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"-> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
