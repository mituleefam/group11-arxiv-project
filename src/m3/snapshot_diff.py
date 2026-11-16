"""Milestone 3, Member 1 - what changed between two versions of the data.

    python src/m3/snapshot_diff.py --old OLD_WORK_DIR --new NEW_WORK_DIR --out diff.csv [--labels LABELS.csv]

OLD_WORK_DIR and NEW_WORK_DIR are two runs of m3_data.py (for instance the
dev data and the data of the Milestone 2 freeze). For every paper, lists the
entries (citation_key) and references (ref_arxiv_id) that were removed,
added, or kept with other content, and with --labels, the labels that point
at something that changed: an entry gone or changed, or a label whose
reference is no longer among the paper's references. Labels are written
against keys, so this says which ones must be checked again.

diff.csv columns: arxiv_id, table (entry/reference), key, change
(removed/added/changed), fields (the fields that differ, for "changed").
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

ENTRY_FIELDS = ("entry_type", "title", "authors", "year", "doi", "journal", "url")
REFERENCE_FIELDS = ("ref_title", "ref_authors", "ref_submission_date", "ref_s2_id")


def _rows(work: Path, name: str, key: str, fields) -> dict:
    t = pq.read_table(work / name, columns=["arxiv_id", key, *fields]).to_pylist()
    return {(r["arxiv_id"], r[key]): r for r in t}


def diff(old: dict, new: dict, fields, table: str) -> list[dict]:
    out = []
    for k in sorted(old.keys() - new.keys()):
        out.append({"arxiv_id": k[0], "table": table, "key": k[1], "change": "removed", "fields": ""})
    for k in sorted(new.keys() - old.keys()):
        out.append({"arxiv_id": k[0], "table": table, "key": k[1], "change": "added", "fields": ""})
    for k in sorted(old.keys() & new.keys()):
        changed = [f for f in fields if old[k].get(f) != new[k].get(f)]
        if changed:
            out.append({"arxiv_id": k[0], "table": table, "key": k[1], "change": "changed", "fields": " ".join(changed)})
    return out


def check_labels(labels: list[dict], changes: list[dict], new_refs: dict) -> list[dict]:
    entry_change = {(c["arxiv_id"], c["key"]): c["change"] for c in changes if c["table"] == "entry"}
    out = []
    for r in labels:
        pid, key, ref = str(r["arxiv_id"]), str(r["citation_key"]), r.get("label_ref_arxiv_id") or None
        why = []
        if (pid, key) in entry_change:
            why.append(f"entry {entry_change[(pid, key)]}")
        if ref and (pid, ref) not in new_refs:
            why.append("reference no longer crawled for this paper")
        if why:
            out.append({"arxiv_id": pid, "citation_key": key, "label_ref_arxiv_id": ref or "", "why": "; ".join(why)})
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Compare two runs of m3_data.py")
    p.add_argument("--old", type=Path, required=True)
    p.add_argument("--new", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True, help="diff CSV")
    p.add_argument("--labels", type=Path, help="label file: also write <out>_labels.csv with the labels to check")
    args = p.parse_args(argv)
    old_e = _rows(args.old, "entries_clean.parquet", "citation_key", ENTRY_FIELDS)
    new_e = _rows(args.new, "entries_clean.parquet", "citation_key", ENTRY_FIELDS)
    old_r = _rows(args.old, "references_clean.parquet", "ref_arxiv_id", REFERENCE_FIELDS)
    new_r = _rows(args.new, "references_clean.parquet", "ref_arxiv_id", REFERENCE_FIELDS)
    changes = diff(old_e, new_e, ENTRY_FIELDS, "entry") + diff(old_r, new_r, REFERENCE_FIELDS, "reference")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["arxiv_id", "table", "key", "change", "fields"])
        w.writeheader()
        w.writerows(changes)
    count = Counter((c["table"], c["change"]) for c in changes)
    print(f"entries: same key {len(old_e.keys() & new_e.keys())}, removed {count[('entry', 'removed')]}, "
          f"added {count[('entry', 'added')]}, changed {count[('entry', 'changed')]}")
    print(f"references: same key {len(old_r.keys() & new_r.keys())}, removed {count[('reference', 'removed')]}, "
          f"added {count[('reference', 'added')]}, changed {count[('reference', 'changed')]}")
    print(f"papers touched: {len({c['arxiv_id'] for c in changes})} -> {args.out}")
    if args.labels:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from split import read_rows
        bad = check_labels(read_rows(args.labels), changes, new_r)
        target = args.out.with_name(args.out.stem + "_labels.csv")
        with open(target, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["arxiv_id", "citation_key", "label_ref_arxiv_id", "why"])
            w.writeheader()
            w.writerows(bad)
        print(f"labels to check again: {len(bad)} -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
