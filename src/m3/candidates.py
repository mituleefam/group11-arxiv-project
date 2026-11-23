"""Milestone 3, Member 1 - candidate pairs and per-paper statistics.

    python src/m3/candidates.py --work WORK_DIR [--papers 2604.05536 ...]

Reads entries_clean.parquet and references_clean.parquet (m3_data.py) and writes:
  candidates.parquet   the m x n candidate matrix of every paper as rows
                       (arxiv_id, citation_key, ref_arxiv_id): every entry of the
                       paper paired with every reference of the same paper
  paper_stats.csv      per paper: m, n, m*n and what makes a paper easy or hard to
                       label (entries naming an arXiv id found among its references,
                       DOIs found, entries without title, misc entries)

A paper with no reference (n = 0) has no candidate row: all its entries are
"no match". The rows are written paper by paper in row groups, so a range of
5000 papers (about 6 million pairs) does not have to fit in memory as Python
objects at once.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

CANDIDATE_SCHEMA = pa.schema([("arxiv_id", pa.string()), ("citation_key", pa.string()),
                              ("ref_arxiv_id", pa.string())])
STAT_FIELDS = ["arxiv_id", "m", "n", "pairs", "entries_with_arxiv_id", "arxiv_id_hits", "entries_with_doi",
               "doi_hits", "entries_without_title", "misc_entries"]
ROW_GROUP = 500_000


def build(work: Path, papers: set[str] | None = None) -> list[dict]:
    entries = pq.read_table(work / "entries_clean.parquet",
                            columns=["arxiv_id", "citation_key", "entry_type", "title_clean", "doi",
                                     "arxiv_id_in_entry"]).to_pylist()
    refs = pq.read_table(work / "references_clean.parquet", columns=["arxiv_id", "ref_arxiv_id", "doi"]).to_pylist()
    by_paper_e, by_paper_r = defaultdict(list), defaultdict(list)
    for e in entries:
        if papers is None or e["arxiv_id"] in papers:
            by_paper_e[e["arxiv_id"]].append(e)
    for r in refs:
        if papers is None or r["arxiv_id"] in papers:
            by_paper_r[r["arxiv_id"]].append(r)

    stats = []
    tmp = work / "candidates.parquet.tmp"
    writer = pq.ParquetWriter(tmp, CANDIDATE_SCHEMA, compression="zstd")
    buf = {"arxiv_id": [], "citation_key": [], "ref_arxiv_id": []}

    def flush():
        if buf["arxiv_id"]:
            writer.write_table(pa.table(buf, schema=CANDIDATE_SCHEMA))
            for v in buf.values():
                v.clear()

    try:
        for pid in sorted(set(by_paper_e) | set(by_paper_r)):
            es, rs = by_paper_e.get(pid, []), by_paper_r.get(pid, [])
            ref_ids = [r["ref_arxiv_id"] for r in rs]
            ref_set, ref_dois = set(ref_ids), {r["doi"] for r in rs if r["doi"]}
            stats.append({
                "arxiv_id": pid, "m": len(es), "n": len(rs), "pairs": len(es) * len(rs),
                "entries_with_arxiv_id": sum(1 for e in es if e["arxiv_id_in_entry"]),
                "arxiv_id_hits": sum(1 for e in es if e["arxiv_id_in_entry"] in ref_set),
                "entries_with_doi": sum(1 for e in es if e["doi"]),
                "doi_hits": sum(1 for e in es if e["doi"] and e["doi"] in ref_dois),
                "entries_without_title": sum(1 for e in es if not e["title_clean"]),
                "misc_entries": sum(1 for e in es if e["entry_type"] == "misc"),
            })
            if not es or not rs:
                continue
            for e in es:
                buf["arxiv_id"] += [pid] * len(ref_ids)
                buf["citation_key"] += [e["citation_key"]] * len(ref_ids)
                buf["ref_arxiv_id"] += ref_ids
            if len(buf["arxiv_id"]) >= ROW_GROUP:
                flush()
        flush()
    finally:
        writer.close()
    tmp.replace(work / "candidates.parquet")
    with open(work / "paper_stats.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=STAT_FIELDS)
        w.writeheader()
        w.writerows(stats)
    return stats


def summary(stats: list[dict]) -> list[str]:
    usable = [s for s in stats if s["m"] and s["n"]]
    lines = [f"papers {len(stats)}: with entries and references {len(usable)}, "
             f"entries but no reference {sum(1 for s in stats if s['m'] and not s['n'])}, "
             f"references but no entry {sum(1 for s in stats if s['n'] and not s['m'])}"]
    if usable:
        pairs = [s["pairs"] for s in usable]
        lines.append(f"candidate pairs {sum(pairs)}: per paper median {statistics.median(pairs):.0f}, max {max(pairs)}; "
                     f"m median {statistics.median(s['m'] for s in usable):.0f}, "
                     f"n median {statistics.median(s['n'] for s in usable):.0f}")
        lines.append(f"papers with m >= 20 and n > 0 (manual labelling needs 20 entries): "
                     f"{sum(1 for s in usable if s['m'] >= 20)}")
    return lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Write the m x n candidate pairs of every paper")
    p.add_argument("--work", type=Path, required=True, help="work folder written by m3_data.py")
    p.add_argument("--papers", nargs="+", help="only these papers (arXiv ids)")
    args = p.parse_args(argv)
    stats = build(args.work, set(args.papers) if args.papers else None)
    for line in summary(stats):
        print(line)
    print(f"written: {args.work / 'candidates.parquet'}, {args.work / 'paper_stats.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
