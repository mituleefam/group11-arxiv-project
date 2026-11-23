"""Milestone 3, modelling - MRR@5 of every scorer on one part of the split.

    python src/m3/modeling/evaluate.py --work WORK [--part valid|test]

Default part: valid. The test set is read only when --part test is given, once,
at the end, after every choice (features, model, parameters) was made on valid.

MRR@5 = mean over the labelled entries of the part (is_sample = 1, the |Q| of
the statement: an entry without a matched reference is not a sample) of
1 / rank of the labelled reference among the candidates of its publication,
0 when it is not in the top 5. Also Hit@1 and Hit@5.

Reported for both baselines (rules, title) and every saved model, on all,
manual and automatic publications, as is and with every link feature masked
("no link": the entry as if it named no arXiv id and no DOI). On automatic
publications the answer was produced by the rules themselves, so a score near
1 there is a sanity check, not an achievement: the manual publications, and
the no-link scores, are the honest measures.

Writes WORK/evaluation_<part>.json and prints a table.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Pairs  # noqa: E402
from models import BASELINES, baseline_scores, load, score  # noqa: E402
from train import evaluate_scorer  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--part", choices=("valid", "test"), default="valid")
    args = p.parse_args(argv)
    folder = args.work / "models"
    if not (folder / "chosen.json").exists():
        print(f"skipped: no trained model in {folder}")
        return 0
    chosen = json.loads((folder / "chosen.json").read_text(encoding="utf-8"))["model"]
    pairs = Pairs(args.work)
    result = {"part": args.part, "chosen": chosen, "scorers": {}}
    for name in BASELINES:
        result["scorers"][name] = evaluate_scorer(pairs, args.part, lambda rows, m, n=name: baseline_scores(pairs, rows, n, m))
    for meta_file in sorted(folder.glob("*.json")):
        name = meta_file.stem
        if name == "chosen":
            continue
        model, meta = load(folder, name)
        result["scorers"][name] = evaluate_scorer(
            pairs, args.part, lambda rows, m, mo=model, me=meta: score(mo, me["kind"], pairs.matrix(rows, me["features"], m)))
    out = args.work / f"evaluation_{args.part}.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    first = result["scorers"]["rules"]
    print(f"{args.part}: {first['all']['entries']} labelled entries "
          f"({first.get('manual', {}).get('entries', 0)} manual, {first.get('auto', {}).get('entries', 0)} automatic)")
    print(f"{'':14}{'MRR@5 all':>11}{'manual':>9}{'auto':>9}{'| no link: all':>15}{'manual':>9}{'auto':>9}{'| Hit@1 manual':>15}")
    for name, v in result["scorers"].items():
        row = [v.get(k, {}).get("mrr5", float("nan")) for k in ("all", "manual", "auto", "all_nolink", "manual_nolink", "auto_nolink")]
        print(f"{name:14}{row[0]:11.4f}{row[1]:9.4f}{row[2]:9.4f}{row[3]:15.4f}{row[4]:9.4f}{row[5]:9.4f}"
              f"{v.get('manual', {}).get('hit1', float('nan')):15.4f}{'   <- chosen' if name == chosen else ''}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
