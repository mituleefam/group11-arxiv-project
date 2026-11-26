"""Milestone 3, context experiment - step 2: sure (entry -> arXiv reference) links in every paper of the corpus.

    python src/m3/context/corpus_links.py --work WORK [--out WORK/context/links.parquet]

The candidates of an entry are described by what OTHER papers of the corpus
write when they cite them (their citation contexts, contexts.py). For that,
the entries of every paper (not only the labelled ones) must be linked to
their arXiv reference. Only the exact rules of auto_label.py are used, R1 same
arXiv id, R2 same DOI, R3 same title (with its year / author checks and the
id_mismatch check), never R4 (close title) nor a trained model: the links
must be sure, and fixed before any training. The labels (manual or not) are
not used here, so the test labels never reach the features.

Writes arxiv_id, citation_key, ref_arxiv_id, rule.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auto_label import Paper, label_paper, load  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    t0 = time.perf_counter()
    entries, refs = load(args.work, None)
    rows = []
    for a, es in entries.items():
        if not refs.get(a):
            continue
        for x in label_paper(es, Paper(refs[a]), threshold=1.01, margin=1.0):  # threshold > 1: no R4
            rule = x["note"].split()[0] if x["note"] else ""
            if x["label_ref_arxiv_id"] and rule in ("R1", "R2", "R3"):
                rows.append({"arxiv_id": a, "citation_key": x["citation_key"],
                             "ref_arxiv_id": x["label_ref_arxiv_id"], "rule": rule})
    out = args.out or args.work / "context" / "links.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), out, compression="zstd")
    n_entries = sum(len(v) for v in entries.values())
    print(f"{len(rows)} sure links of {n_entries} entries in {len({r['arxiv_id'] for r in rows})} papers, "
          f"{len({r['ref_arxiv_id'] for r in rows})} distinct references -> {out} ({time.perf_counter() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
