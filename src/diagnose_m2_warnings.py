"""Evidence-based diagnosis of five Milestone 2 warning families.

Reads existing M2 outputs and (when available) M1 source. NEVER edits or
re-parses the corpus to create new outputs; it does not mark warnings fixed.

    python src/diagnose_m2_warnings.py --data-dir data
    python src/diagnose_m2_warnings.py --data-dir data --paper-ids 2604.07041 2604.08146

Writes logs/warning_evidence_m2.csv and logs/warning_evidence_summary_m2.json.
Every row keeps the original status and separates observations from hypotheses.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from gatherer import gather, read_text
from hierarchy import document_body
from reference_parser.bibitem_parser import iter_cite_groups, parse_amsrefs, parse_bib, parse_bibitems

CHECKS = {
    "empty_tex_tree", "citation_key_coverage", "gathering_problems",
    "nonstandard_leaf_kinds", "encoding_replacement_chars",
}
REPORT_FIELDS = ("arxiv_id", "version", "check_name", "status", "classification",
                 "priority", "evidence", "next_action")
MAX_SCAN_BYTES = 12_000_000
MAX_SOURCE_FILES = 200
MAX_EXAMPLES = 12
ROOT_PATTERN = re.compile(r"\d+ files with \\documentclass:")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _version_label(value: str) -> str:
    value = str(value or "")
    return value if value.startswith("v") else f"v{value}"


def _paper_path(root: Path, paper_id: str) -> Path:
    return root / "corpus" / paper_id.replace(".", "-").replace("/", "-")


def _paper_jsons(folder: Path) -> tuple[dict, dict, dict, dict]:
    parsed = folder / "parsed"
    elements = _load_json(parsed / "elements.json") or {}
    hierarchy = _load_json(parsed / "hierarchy.json") or {}
    bibtex = _load_json(parsed / "bibtex.json") or {}
    gathering = _load_json(parsed / "gathering.json") or {}
    return elements, hierarchy, bibtex, gathering


def _cited_keys(elements: dict) -> set[str]:
    found: set[str] = set()
    for row in elements.get("rows", []):
        if not isinstance(row, list) or len(row) != 3 or not isinstance(row[2], str):
            continue
        text = row[2]
        for start, end in iter_cite_groups(text):
            found.update(k.strip() for k in text[start:end].split(",") if k.strip() and k.strip() != "*")
    return found


def _source_bib_keys(folder: Path) -> tuple[set[str], list[str]]:
    """Read definitions, not arbitrary substring matches (e.g. title containing a key).

    This is evidence about source definitions, not a second canonical parser.
    .bbl sources are included if they were actually restored under corpus.
    """
    found: set[str] = set()
    notes = []
    candidates = []
    try:
        candidates = sorted(p for p in folder.rglob("*") if p.suffix.lower() in {".bib", ".bbl"}
                            and p.is_file() and not p.is_symlink())
    except OSError as exc:
        return found, [f"could_not_list_sources: {exc}"]
    if len(candidates) > MAX_SOURCE_FILES:
        notes.append(f"source_file_limit_exceeded: {len(candidates)} files")
        candidates = candidates[:MAX_SOURCE_FILES]
    for path in candidates:
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                notes.append(f"file_too_large: {path.relative_to(folder)}")
                continue
            content, _ = read_text(path)
            if path.suffix.lower() == ".bib":
                entries = parse_bib(content, str(path))
            else:
                entries = parse_bibitems(content) + parse_amsrefs(content)
            found.update(e["citation_key"] for e in entries if e.get("citation_key"))
        except (OSError, UnicodeError, ValueError, KeyError) as exc:
            notes.append(f"could_not_parse: {path.relative_to(folder)}: {type(exc).__name__}")
    return found, notes


def _citation_evidence(folder: Path, elements: dict, bib: dict) -> tuple[str, str, dict]:
    cited = _cited_keys(elements)
    table_keys = {r.get("citation_key") for r in bib.get("rows", [])
                  if isinstance(r, dict) and isinstance(r.get("citation_key"), str)}
    missing = sorted(cited - table_keys)
    source_keys, notes = _source_bib_keys(folder)
    folded_table = defaultdict(set)
    for k in table_keys:
        folded_table[k.casefold()].add(k)
    exact_in_source = [k for k in missing if k in source_keys]
    case_only_in_table = {k: sorted(folded_table[k.casefold()]) for k in missing
                          if k.casefold() in folded_table and k not in table_keys}
    case_only_in_source = {k: sorted(s for s in source_keys if s.casefold() == k.casefold())
                           for k in missing if k not in source_keys and
                           any(s.casefold() == k.casefold() for s in source_keys)}
    example = {"cited_keys": len(cited), "table_keys": len(table_keys),
               "unmatched_count": len(missing), "unmatched_examples": missing[:20],
               "source_defined_but_not_exported": exact_in_source[:20],
               "case_only_in_table": dict(list(case_only_in_table.items())[:20]),
               "case_only_in_source": dict(list(case_only_in_source.items())[:20]),
               "source_notes": notes[:MAX_EXAMPLES]}
    if exact_in_source:
        return "source_defined_missing_from_output", "high", example
    if case_only_in_table or case_only_in_source:
        return "citation_case_or_alias_mismatch", "high", example
    if missing and not table_keys:
        return "no_exported_bibliography", "high", example
    if missing:
        return "unresolved_source_citations", "review", example
    return "citation_keys_match", "info", example


def _gather_evidence(info: dict) -> tuple[str, str, dict]:
    issues = [str(p) for p in info.get("problems", [])]
    missing = list(info.get("missing", [])) + list(info.get("bib_names_missing", []))
    example = {"status": info.get("status"), "root": info.get("root"),
               "files_count": len(info.get("files", [])),
               "missing": missing[:MAX_EXAMPLES], "problems": issues[:MAX_EXAMPLES],
               "unused_tex_count": len(info.get("unused", []))}
    if missing:
        return "missing_source_dependency", "high", example
    if issues and all(ROOT_PATTERN.search(p) for p in issues):
        return "multiple_root_candidates", "info", example
    if any("guessed as the root" in p for p in issues):
        return "root_guessed_without_documentclass", "review", example
    if any("fallback" in p or "references only" in p for p in issues):
        return "bibliography_fallback_or_missing", "review", example
    return "other_gathering_problem", "review", example


def _empty_tree_evidence(folder: Path, version: str, gathering: dict) -> tuple[str, str, dict]:
    info = gathering.get(_version_label(version), {})
    subfolder = folder / _version_label(version)
    root = info.get("root")
    observed = {"gather_status": info.get("status"), "root": root,
                "compiled_chars_in_log": info.get("chars"),
                "missing": info.get("missing", []),
                "root_exists": bool(root and (subfolder / root).is_file()),
                "problems": info.get("problems", [])[:MAX_EXAMPLES]}
    if info.get("status") != "ok":
        return "no_tex_or_unavailable_source", "review", observed
    if not observed["root_exists"]:
        return "missing_selected_root", "high", observed
    try:
        compiled = gather(subfolder, "success")
        from cleaner import clean
        from normalizer import normalize
        from leaf_extractor import extract_leaves
        normalized = normalize(clean(compiled.text))
        body = document_body(normalized)
        # Diagnostic only: extracted leaves here omit the actual hierarchy's
        # bibliography removal and masking, so this is NOT a proof of lost text.
        leaves = extract_leaves(body) if len(body) <= 2_000_000 else []
        observed.update(compiled_length=len(compiled.text), cleaned_body_length=len(body),
                        standalone_leaves=len(leaves),
                        stand_alone_probe_skipped=len(body) > 2_000_000)
        if leaves:
            return "content_present_but_no_hierarchy_edges", "high", observed
        if body.strip():
            return "body_present_without_extractable_leaves", "high", observed
        return "empty_compiled_body", "review", observed
    except (OSError, ValueError, RuntimeError) as exc:
        observed["probe_exception"] = f"{type(exc).__name__}: {exc}"
        return "empty_tree_probe_inconclusive", "high", observed


def _nonstandard_leaf_evidence(elements: dict, hierarchy: dict, version: str) -> tuple[str, str, dict]:
    item_map = {r[0]: (r[1], r[2]) for r in elements.get("rows", [])
                if isinstance(r, list) and len(r) == 3}
    try:
        v = int(str(version).removeprefix("v"))
    except ValueError:
        return "unknown_version", "review", {"version": version}
    children, present = Counter(), set()
    for row in hierarchy.get("rows", []):
        if isinstance(row, list) and len(row) == 3 and row[0] == v:
            present.add(row[1]); present.add(row[2]); children[row[2]] += 1
    leaf = [(node, *item_map[node]) for node in present if node in item_map
            and children[node] == 0 and item_map[node][0] not in {"sentence", "formula", "figure", "document"}]
    counts = Counter(kind for _, kind, _ in leaf)
    return "empty_structural_nodes_need_source_review", "review", {
        "unexpected_leaf_count": len(leaf), "kinds": dict(counts),
        "examples": [{"element_id": eid, "kind": kind, "content": text[:160]}
                     for eid, kind, text in sorted(leaf)[:MAX_EXAMPLES]]}


def _encoding_evidence(folder: Path, elements: dict, bib: dict) -> tuple[str, str, dict]:
    output_hits = []
    for row in elements.get("rows", []):
        if isinstance(row, list) and len(row) == 3 and "\ufffd" in str(row[2]):
            output_hits.append({"element_id": row[0], "context": str(row[2])[:150]})
    for row in bib.get("rows", []):
        if isinstance(row, dict) and "\ufffd" in str(row):
            output_hits.append({"bibtex_key": row.get("citation_key"), "context": str(row)[:150]})
    source_found, files_seen, errors = [], 0, []
    try:
        for p in folder.rglob("*"):
            if not p.is_file() or p.is_symlink() or p.suffix.lower() not in {".tex", ".bib", ".bbl"}:
                continue
            files_seen += 1
            if files_seen > MAX_SOURCE_FILES:
                errors.append("source_file_limit_exceeded")
                break
            if p.stat().st_size > MAX_SCAN_BYTES:
                errors.append(f"file_too_large: {p.relative_to(folder)}")
                continue
            text, _ = read_text(p)
            if "\ufffd" in text:
                source_found.append(str(p.relative_to(folder)))
    except (OSError, UnicodeError) as exc:
        errors.append(f"could_not_scan_all_source: {type(exc).__name__}")
    return ("replacement_character_present_in_source" if source_found else
            "replacement_character_origin_unknown", "high", {
                "output_hits": output_hits[:MAX_EXAMPLES],
                "source_files_containing_U+FFFD": source_found[:MAX_EXAMPLES],
                "source_files_scanned": min(files_seen, MAX_SOURCE_FILES),
                "source_notes": errors[:MAX_EXAMPLES]})


def analyze(row: dict, data_dir: Path) -> dict:
    paper_id = str(row.get("arxiv_id", ""))
    kind = str(row.get("check_name", ""))
    folder = _paper_path(data_dir, paper_id)
    elements, hierarchy, bib, gathering = _paper_jsons(folder)
    priority = "review"
    try:
        if kind == "citation_key_coverage":
            classification, priority, evidence = _citation_evidence(folder, elements, bib)
            action = "Compare unexported keys against .bib/.bbl and cite_rename_map; fix only verified parser losses"
        elif kind == "empty_tex_tree":
            classification, priority, evidence = _empty_tree_evidence(folder, row.get("version", ""), gathering)
            action = "Inspect selected root and compiled body, then reproduce in a one-paper regression test"
        elif kind == "gathering_problems":
            classification, priority, evidence = _gather_evidence(gathering.get(_version_label(row.get("version", "")), {}))
            action = "Inspect root choice only for ambiguous or missing files; multiple candidates alone are informational"
        elif kind == "nonstandard_leaf_kinds":
            classification, priority, evidence = _nonstandard_leaf_evidence(elements, hierarchy, row.get("version", ""))
            action = "Review listed structural nodes against source; preserve intentional empty headings and items"
        elif kind == "encoding_replacement_chars":
            classification, priority, evidence = _encoding_evidence(folder, elements, bib)
            action = "Compare raw bytes with output; never replace U+FFFD by guessing the intended character"
        else:
            classification, evidence, action = "unhandled", {}, ""
    except (OSError, TypeError, KeyError, ValueError, RuntimeError) as exc:
        classification, priority, evidence = "diagnostic_error", "high", {"exception": f"{type(exc).__name__}: {exc}"}
        action = "Check paper files and rerun diagnosis; keep original warning"
    return {"arxiv_id": paper_id, "version": str(row.get("version", "")),
            "check_name": kind, "status": str(row.get("status", "")),
            "classification": classification, "priority": priority,
            "evidence": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            "next_action": action}


def run(data_dir: Path, *, source: Path | None = None, out: Path | None = None,
        ids: set[str] | None = None) -> dict:
    source = source or data_dir / "logs" / "validation_report_m2.csv"
    out = out or data_dir / "logs" / "warning_evidence_m2.csv"
    if not source.is_file():
        raise FileNotFoundError(source)
    counters = Counter()
    classifications = Counter()
    rows = []
    with source.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"arxiv_id", "version", "check_name", "status", "details"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Missing columns: {sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            if row["status"] not in {"WARN", "FAIL"} or row["check_name"] not in CHECKS:
                continue
            if ids and row["arxiv_id"] not in ids and row["arxiv_id"].replace(".", "-") not in ids:
                continue
            enriched = analyze(row, data_dir)
            rows.append(enriched)
            counters[row["check_name"]] += 1
            classifications[enriched["classification"]] += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        w.writeheader(); w.writerows(rows)
    summary = {"source_validation_report": str(source), "analyzed_checks": len(rows),
               "by_check": dict(counters), "by_classification": dict(classifications),
               "note": "Evidence-based triage only. The input corpus and validation statuses are NOT changed."}
    output_summary = out.with_name("warning_evidence_summary_m2.json")
    output_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--validation-csv", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--paper-ids", nargs="*", help="Optional list of paper IDs to inspect")
    args = ap.parse_args(argv)
    summary = run(args.data_dir, source=args.validation_csv, out=args.out,
                  ids=set(args.paper_ids) if args.paper_ids else None)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
