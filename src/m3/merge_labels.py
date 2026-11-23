"""Milestone 3, labelling step 4 - one label file for the group, checked.

    python src/m3/merge_labels.py --work WORK [--manual WORK/labels_manual_*.csv] [--auto WORK/labels_auto.csv]
                                  [--suspects FILE] [--allow-incomplete] [--out WORK/labels.csv]

Joins the manual label files (label_sheet.py import) and the automatic ones
(auto_label.py) into WORK/labels.csv, same columns plus `range`:
    arxiv_id, citation_key, label_ref_arxiv_id, is_match, source, labeler, note, range
Every decided entry is kept, also is_match = false: the predictions table has
one row per BibTeX entry of each publication used for matching, with a null
groundtruth_arxiv_id when there is no reference.

Checks (an error stops the script, nothing is written):
  - same columns, source manual / auto, is_match true / false agreeing with
    label_ref_arxiv_id;
  - every entry exists in entries_clean.parquet and appears once;
  - every label is a reference (with arXiv id) of the same publication;
  - no publication is both manual and automatic, and no manual pick is in
    the automatic labels (the lecturer: a publication labelled by hand never
    goes through the automatic labelling);
  - per range (select_manual.GROUP_RANGES): >= 5 manual publications, each
    with >= 20 labels (matched pairs), >= 100 manual labels, and automatic
    publications >= 10% of the range outside the manual pool.
The per-range minimums are errors unless --allow-incomplete (while the other
members are still labelling): they are then printed as warnings.

Also writes:
  WORK/label_summary.csv   per range: publications and labels, manual / auto
  WORK/split_rankable.csv  arxiv_id, n for split.py --stats: n = 0 for the
                           publications that must not go to test or valid
                           (no reference, no label at all, or a suspect
                           automatic label from --suspects)

How the labels are used (the lecturer's Q&A): a label is a matched pair; an
entry with is_match = false is not a sample, neither for training nor for
MRR@5 (|Q| counts the is_match = true entries of the test set). Training
pairs come from the is_match = true entries only: 1 yes (the label) and the
other references of the same publication as no. The candidates of an
is_match = false entry are never used as negatives: the automatic rules miss
some true pairs on purpose (precision first).

Then:  python src/m3/split.py --labels WORK/labels.csv --members 5 --seed 11 \\
           --stats WORK/split_rankable.csv --out WORK/splits.json
"""

from __future__ import annotations

import argparse
import csv
import glob
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from select_manual import GROUP_RANGES, range_of  # noqa: E402

LABEL_COLS = ["arxiv_id", "citation_key", "label_ref_arxiv_id", "is_match", "source", "labeler", "note"]
OUT_COLS = LABEL_COLS + ["range"]
MIN_MANUAL_PAPERS, MIN_PAIRS_PER_PAPER, MIN_PAIRS_PER_MEMBER, AUTO_FRACTION = 5, 20, 100, 0.10


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def merge(work: Path, manual_files: list[Path], auto_file: Path | None, suspects: set[str],
          allow_incomplete: bool) -> tuple[list[dict], list[str], list[str], list[dict], list[dict]]:
    errors, warnings, rows = [], [], []
    for path in manual_files + ([auto_file] if auto_file else []):
        part = read_csv(path)
        missing = [c for c in LABEL_COLS if part and c not in part[0]]
        if missing:
            errors.append(f"{path.name}: missing columns {missing}")
            continue
        for r in part:
            rows.append({c: (r.get(c) or "").strip() for c in LABEL_COLS} | {"_file": path.name})
    if errors:
        return [], errors, warnings, [], []

    papers = {r["arxiv_id"] for r in rows}
    entries = {(e["arxiv_id"], e["citation_key"]) for e in pq.read_table(
        work / "entries_clean.parquet", columns=["arxiv_id", "citation_key"]).to_pylist() if e["arxiv_id"] in papers}
    refs = defaultdict(set)
    for r in pq.read_table(work / "references_clean.parquet", columns=["arxiv_id", "ref_arxiv_id"]).to_pylist():
        if r["arxiv_id"] in papers:
            refs[r["arxiv_id"]].add(r["ref_arxiv_id"])

    seen = Counter((r["arxiv_id"], r["citation_key"]) for r in rows)
    errors += [f"{p} {k}: {n} rows (files: {sorted({r['_file'] for r in rows if (r['arxiv_id'], r['citation_key']) == (p, k)})})"
               for (p, k), n in seen.items() if n > 1][:50]
    for r in rows:
        key = f"{r['_file']}: {r['arxiv_id']} {r['citation_key']}"
        if r["source"] not in ("manual", "auto"):
            errors.append(f"{key}: source {r['source']!r}")
        if r["is_match"] not in ("true", "false") or (r["is_match"] == "true") != bool(r["label_ref_arxiv_id"]):
            errors.append(f"{key}: is_match {r['is_match']!r} with label {r['label_ref_arxiv_id']!r}")
        if (r["arxiv_id"], r["citation_key"]) not in entries:
            errors.append(f"{key}: not an entry of entries_clean.parquet")
        if r["label_ref_arxiv_id"] and r["label_ref_arxiv_id"] not in refs[r["arxiv_id"]]:
            errors.append(f"{key}: {r['label_ref_arxiv_id']} is not a reference of {r['arxiv_id']}")
        r["range"] = range_of(r["arxiv_id"]) or ""
        if not r["range"]:
            errors.append(f"{key}: publication outside the group's ranges")

    source_of = defaultdict(set)
    for r in rows:
        source_of[r["arxiv_id"]].add(r["source"])
    both = sorted(p for p, s in source_of.items() if len(s) > 1)
    if both:
        errors.append(f"{len(both)} publications are both manual and automatic: {both[:10]}")
    picks_file = work / "manual_pick.csv"
    picks = {r["arxiv_id"] for r in read_csv(picks_file)} if picks_file.exists() else set()
    auto_picked = sorted(p for p, s in source_of.items() if "auto" in s and p in picks)
    if auto_picked:
        errors.append(f"manual picks with automatic labels: {auto_picked[:10]}")
    manual_not_picked = sorted(p for p, s in source_of.items() if "manual" in s and picks and p not in picks)
    if manual_not_picked:
        warnings.append(f"manual publications not in manual_pick.csv: {manual_not_picked[:10]}")

    # per range
    status = {r["arxiv_id"]: r for r in read_csv(work / "papers_refs.csv")}
    labels_of = Counter(r["arxiv_id"] for r in rows if r["is_match"] == "true")
    summary = []
    for name in GROUP_RANGES:
        in_range = [p for p in status if range_of(p) == name]
        if not in_range:
            continue
        manual = sorted(p for p, s in source_of.items() if s == {"manual"} and range_of(p) == name)
        auto = sorted(p for p, s in source_of.items() if s == {"auto"} and range_of(p) == name)
        manual_labels = sum(labels_of[p] for p in manual)
        want_auto = math.ceil(AUTO_FRACTION * (len(in_range) - len([p for p in picks if range_of(p) == name] or manual)))
        summary.append({"range": name, "publications": len(in_range), "manual_publications": len(manual),
                        "manual_entries": sum(1 for r in rows if r["arxiv_id"] in set(manual)),
                        "manual_labels": manual_labels, "auto_publications": len(auto),
                        "auto_entries": sum(1 for r in rows if r["arxiv_id"] in set(auto)),
                        "auto_labels": sum(labels_of[p] for p in auto), "auto_wanted": want_auto})
        short = []
        if len(manual) < MIN_MANUAL_PAPERS:
            short.append(f"{len(manual)} manual publications (>= {MIN_MANUAL_PAPERS})")
        short += [f"{p}: {labels_of[p]} labels (>= {MIN_PAIRS_PER_PAPER})" for p in manual
                  if labels_of[p] < MIN_PAIRS_PER_PAPER]
        if manual_labels < MIN_PAIRS_PER_MEMBER:
            short.append(f"{manual_labels} manual labels (>= {MIN_PAIRS_PER_MEMBER})")
        if len(auto) < want_auto:
            short.append(f"{len(auto)} automatic publications (>= {want_auto})")
        for s in short:
            (warnings if allow_incomplete else errors).append(f"{name}: {s}")

    stats = {r["arxiv_id"]: int(r["n"]) for r in read_csv(work / "paper_stats.csv")}
    rankable = []
    for p in sorted(source_of):
        n = stats.get(p, 0)
        if labels_of[p] == 0 or p in suspects:
            n = 0
        rankable.append({"arxiv_id": p, "n": n})
    for r in rows:
        r.pop("_file", None)
    return rows, errors, warnings, summary, rankable


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--manual", type=Path, nargs="*", help="manual label files (default WORK/labels_manual_*.csv)")
    p.add_argument("--auto", type=Path, help="automatic labels (default WORK/labels_auto.csv)")
    p.add_argument("--suspects", type=Path,
                   help="publications not to put in test / valid (a .csv with arxiv_id, or one id per line)")
    p.add_argument("--allow-incomplete", action="store_true", help="per-range minimums as warnings")
    p.add_argument("--out", type=Path, help="default WORK/labels.csv")
    args = p.parse_args(argv)

    manual = args.manual if args.manual else [Path(x) for x in sorted(glob.glob(str(args.work / "labels_manual_*.csv")))]
    auto = args.auto or (args.work / "labels_auto.csv")
    suspects = set()
    if args.suspects:
        text = args.suspects.read_text(encoding="utf-8")
        suspects = ({r["arxiv_id"] for r in csv.DictReader(text.splitlines())} if args.suspects.suffix == ".csv"
                    else {x.strip() for x in text.splitlines() if x.strip()})
    print(f"manual files: {[m.name for m in manual]}; automatic: {auto.name if auto.exists() else '-'}")
    rows, errors, warnings, summary, rankable = merge(args.work, manual, auto if auto.exists() else None,
                                                      suspects, args.allow_incomplete)
    for w in warnings:
        print("!! " + w)
    if errors:
        print(f"{len(errors)} errors, nothing written:")
        for e in errors[:100]:
            print("  " + e)
        return 1
    out = args.out or args.work / "labels.csv"
    for path, cols, data in ((out, OUT_COLS, rows),
                             (args.work / "label_summary.csv", list(summary[0]) if summary else ["range"], summary),
                             (args.work / "split_rankable.csv", ["arxiv_id", "n"], rankable)):
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(data)
    for s in summary:
        print(f"  {s['range']}: manual {s['manual_publications']} publications / {s['manual_labels']} labels, "
              f"auto {s['auto_publications']} publications / {s['auto_labels']} labels")
    print(f"{len(rows)} entries, {sum(1 for r in rows if r['is_match'] == 'true')} labels, "
          f"{len({r['arxiv_id'] for r in rows})} publications -> {out}; "
          f"{sum(1 for r in rankable if r['n'] == 0)} publications kept out of test/valid -> split_rankable.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
