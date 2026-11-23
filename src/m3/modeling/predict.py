"""Milestone 3, modelling - the predictions table of the statement.

    python src/m3/modeling/predict.py --work WORK [--model NAME] [--out WORK/predictions.parquet]

With the chosen model (WORK/models/chosen.json, or --model), every BibTeX
entry of every publication used for matching (train, valid and test: the
publications of labels.csv) gets its candidates ranked; the 5 best are
cand_1 (best) .. cand_5. One row per entry, columns of the statement:

  arxiv_id               string, the publication the entry belongs to
  partition              train / valid / test (splits.json)
  citation_key           string, the BibTeX entry
  groundtruth_arxiv_id   the labelled reference, null when the entry has none
                         (is_match = false) - such an entry is not a sample
  cand_1 .. cand_5       ranked candidates; null when the publication has
                         fewer than 5 references with an arXiv id

Also a `range` column (the member's ID range) so that each member can take
their own rows for their Hugging Face dataset; drop it when publishing if the
schema must be exactly the statement's.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pairs import Pairs  # noqa: E402
from models import load, score  # noqa: E402
from select_manual import range_of  # noqa: E402

SCHEMA = pa.schema([("arxiv_id", pa.string()), ("partition", pa.dictionary(pa.int8(), pa.string())),
                    ("citation_key", pa.string()), ("groundtruth_arxiv_id", pa.string())] +
                   [(f"cand_{k}", pa.string()) for k in range(1, 6)] + [("range", pa.string())])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--model", help="a model name in WORK/models (default: chosen.json)")
    p.add_argument("--out", type=Path, help="default WORK/predictions.parquet")
    args = p.parse_args(argv)
    folder = args.work / "models"
    if not (folder / "chosen.json").exists() and not args.model:
        print(f"skipped: no trained model in {folder}")
        return 0
    name = args.model or json.loads((folder / "chosen.json").read_text(encoding="utf-8"))["model"]
    model, meta = load(folder, name)
    pairs = Pairs(args.work)
    rows = np.arange(len(pairs.y))
    s = score(model, meta["kind"], pairs.matrix(rows, meta["features"]))

    # best 5 per group: sort by (group, -score, ref) so that ties are broken the same way every run
    ref_code = np.unique(pairs.ref.astype(str), return_inverse=True)[1]
    order = np.lexsort((ref_code, -s, pairs.gidx))
    top = {}
    for i in order:
        g = pairs.group[i]
        lst = top.setdefault(g, [])
        if len(lst) < 5:
            lst.append(pairs.ref[i])
    gold = {pairs.group[i]: pairs.ref[i] for i in np.flatnonzero(pairs.y == 1)}

    splits = json.loads((args.work / "splits.json").read_text(encoding="utf-8"))
    part_of = {pid: part for part in ("train", "valid", "test") for pid in splits[part]}
    with open(args.work / "labels.csv", encoding="utf-8-sig", newline="") as f:
        labels = list(csv.DictReader(f))
    out_rows, without_candidates = [], 0
    for lab in labels:
        g = f"{lab['arxiv_id']}|{lab['citation_key']}"
        cands = top.get(g, [])
        without_candidates += not cands
        truth = lab["label_ref_arxiv_id"] or None
        if truth is not None and cands and gold.get(g) != truth:
            raise ValueError(f"{g}: labels.csv and features.parquet disagree ({truth} / {gold.get(g)})")
        out_rows.append({"arxiv_id": lab["arxiv_id"], "partition": part_of.get(lab["arxiv_id"]),
                         "citation_key": lab["citation_key"], "groundtruth_arxiv_id": truth,
                         **{f"cand_{k + 1}": (cands[k] if k < len(cands) else None) for k in range(5)},
                         "range": range_of(lab["arxiv_id"])})
    missing_part = sum(1 for r in out_rows if r["partition"] is None)
    if missing_part:
        raise ValueError(f"{missing_part} entries of publications not in splits.json")
    out = args.out or args.work / "predictions.parquet"
    pq.write_table(pa.Table.from_pylist(out_rows, schema=SCHEMA), out, compression="zstd")
    print(f"model {name}: {len(out_rows)} entries of {len({r['arxiv_id'] for r in out_rows})} publications "
          f"({sum(r['groundtruth_arxiv_id'] is not None for r in out_rows)} with a groundtruth, "
          f"{without_candidates} without candidate) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
