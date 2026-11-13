"""Classify M2 validation warnings without hiding or rewriting raw results.

Usage:
  python src/audit_m2_warnings.py --data-dir /content/gbao_m2_full_work

Outputs:
  logs/warning_triage_m2.csv     all non-PASS checks, enriched with source details
  logs/warning_summary_m2.json  totals by check and action priority

This tool reports *checks*, not distinct affected papers; a warning is not
proof of a parser defect. Full citation coverage cannot be established from a
missing key alone. Inspect corresponding .tex/.bib/.bbl before code changes.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

COLUMNS = ("arxiv_id", "version", "check_name", "status", "priority",
           "details", "source_problems", "citation_matched", "citation_total",
           "action")
COVERAGE = re.compile(r"matched\s*=\s*(\d+)\s*/\s*(\d+)")


def _problem_details(root: Path, paper_id: str, version: str) -> list[str]:
    if not paper_id or not version:
        return []
    # Input IDs in reports use '.', on-disk corpus IDs use '-'.
    folder_id = paper_id.replace(".", "-").replace("/", "-")
    path = root / "corpus" / folder_id / "parsed" / "gathering.json"
    if not path.is_file():
        return []
    try:
        gathered = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    version_label = str(version)
    if not version_label.startswith("v"):
        version_label = f"v{version_label}"
    result = gathered.get(version_label, {})
    return [str(p) for p in result.get("problems", [])]


def classify(row: dict, root: Path) -> dict:
    """Return one enriched row. No validation PASS/FAIL status is changed."""
    item = {key: str(row.get(key, "")) for key in
            ("arxiv_id", "version", "check_name", "status", "details")}
    kind, status = item["check_name"], item["status"]
    problems = _problem_details(root, item["arxiv_id"], item["version"]) \
        if kind == "gathering_problems" else []
    item.update(priority="review", source_problems=" | ".join(problems),
                citation_matched="", citation_total="", action="Inspect source and parser output")
    if status == "FAIL":
        item.update(priority="critical", action="Resolve FAIL before merging")
    elif kind == "citation_key_coverage":
        match = COVERAGE.search(item["details"])
        if match:
            matched, total = map(int, match.groups())
            item.update(citation_matched=str(matched), citation_total=str(total))
            item["priority"] = "high" if total and matched / total < 0.5 else "review"
        item["action"] = ("Inspect source .tex/.bib/.bbl, parsed/bibtex.json and "
                          "cite_rename_map; verify unresolved keys")
    elif kind == "empty_tex_tree":
        item.update(priority="high", action="Inspect selected root and parsed hierarchy; check source content")
    elif kind == "encoding_replacement_chars":
        item.update(priority="high", action="Inspect original source encoding and U+FFFD positions")
    elif kind == "gathering_problems":
        only_multiple_roots = bool(problems) and all(
            "files with \\documentclass:" in p for p in problems)
        if only_multiple_roots:
            item.update(priority="info", action="Verify selected root once; alternate documentclasses were recorded")
        else:
            item.update(priority="review", action="Inspect gathering.json problems and selected compilation path")
    elif kind == "nonstandard_leaf_kinds":
        item.update(priority="review", action="Inspect empty heading/item/list/paragraph; verify no content was lost")
    elif kind in ("appendix_included", "acknowledgements_included"):
        item.update(priority="review", action="Compare source and hierarchy; heading-name heuristic may be inconclusive")
    return item


def run(root: Path, source: Path | None = None, output: Path | None = None) -> dict:
    root = root.resolve()
    source = source or root / "logs" / "validation_report_m2.csv"
    output = output or root / "logs" / "warning_triage_m2.csv"
    summary_path = output.with_name("warning_summary_m2.json")
    if not source.is_file():
        raise FileNotFoundError(source)
    checks, by_type, by_priority = Counter(), Counter(), Counter()
    paper_ids = set()
    rows = []
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"arxiv_id", "version", "check_name", "status", "details"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Missing validation fields: {sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            checks[row["status"]] += 1
            if row["status"] not in ("WARN", "FAIL"):
                continue
            enriched = classify(row, root)
            rows.append(enriched)
            by_type[enriched["check_name"]] += 1
            by_priority[enriched["priority"]] += 1
            if enriched["arxiv_id"] and enriched["arxiv_id"] != "ALL":
                paper_ids.add(enriched["arxiv_id"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    summary = {"checks_by_status": dict(checks), "warnings_by_check": dict(by_type),
               "warnings_by_priority": dict(by_priority),
               "papers_with_nonpass_checks": len(paper_ids),
               "nonpass_check_count": len(rows),
               "note": "Counts are validation checks, not parser failures or unique papers. "
                       "Priority is triage guidance, not proof of defect."}
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--validation-csv", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    summary = run(args.data_dir, args.validation_csv, args.out)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary["checks_by_status"].get("FAIL", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
