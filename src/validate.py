"""Role 4: audit Group 11 Milestone 1 corpus without making network requests.

Run from repository root:
    python src/validate.py --data-dir /path/to/data
    python src/validate.py --data-dir /path/to/data --strict

The manifest is append-only: the last valid row per arXiv ID wins.
A missing/pending item is not silently counted as a successful crawl.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

ID_RE = re.compile(r"^\d{4}\.\d{4,5}$")
VERSION_RE = re.compile(r"^v[1-9]\d*$")
FINAL_SOURCE = {"success", "pdf_only", "no_tex"}
KNOWN_SOURCE = FINAL_SOURCE | {"not_found", "failed"}
KNOWN_REF = {"success", "empty", "unavailable", "not_found", "api_error"}
COLUMNS = ["arxiv_id", "version", "check_name", "status", "error_message"]


def load_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def iso_valid(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def load_manifest(path: Path):
    """Return last record for each ID and manifest-format issues."""
    records, problems = {}, []
    if not path.is_file():
        return records, [("", "FAIL", "Missing work/paper_manifest.jsonl")]
    with path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict) or not isinstance(rec.get("arxiv_id"), str):
                    raise ValueError("record must be an object with arxiv_id")
            except (json.JSONDecodeError, ValueError) as exc:
                problems.append((str(line_number), "WARN", f"Invalid manifest line: {exc}"))
                continue
            records[rec["arxiv_id"]] = rec
    return records, problems


def source_files(directory: Path):
    """Yield relative file paths, including disallowed extensions, without following symlinks."""
    return [p for p in directory.rglob("*") if p.is_file() or p.is_symlink()]


def validate(data_dir: Path):
    issues = []
    counts = Counter()

    def add(arxiv_id, version, check, status, detail=""):
        issues.append(dict(arxiv_id=str(arxiv_id), version=str(version), check_name=check,
                           status=status, error_message=str(detail)))
        counts[status] += 1

    manifest, manifest_problems = load_manifest(data_dir / "work" / "paper_manifest.jsonl")
    for number, status, detail in manifest_problems:
        add("", number, "manifest", status, detail)
    if not manifest:
        add("", "", "manifest", "FAIL", "No valid manifest records found")
        return issues

    for arxiv_id, rec in sorted(manifest.items()):
        if not ID_RE.fullmatch(arxiv_id):
            add(arxiv_id, "", "paper_id", "FAIL", "Unexpected arXiv identifier")
        if rec.get("folder") != arxiv_id.replace(".", "-"):
            add(arxiv_id, "", "paper_folder", "WARN", "Manifest folder differs from ID")
        paper_status = rec.get("status")
        if paper_status != "success":
            if paper_status == "not_found":
                add(arxiv_id, "", "discovery", "WARN", "ID not found by discovery")
            else:
                add(arxiv_id, "", "discovery", "PENDING", f"Discovery status: {paper_status}")
            continue

        latest = rec.get("latest_version")
        expected = rec.get("versions")
        if not isinstance(latest, int) or latest < 1 or not isinstance(expected, list) or expected != [f"v{i}" for i in range(1, latest + 1)]:
            add(arxiv_id, "", "version_manifest", "FAIL", "Version list inconsistent with latest_version")
            continue
        paper_dir = data_dir / "corpus" / arxiv_id.replace(".", "-")
        if not paper_dir.is_dir():
            add(arxiv_id, "", "paper_dir", "PENDING", "Paper output folder not yet created")
            continue

        # Metadata from Member 1
        metadata_path = paper_dir / "metadata.json"
        if not metadata_path.is_file():
            add(arxiv_id, "", "metadata", "PENDING", "metadata.json not found")
        else:
            try:
                metadata = load_json(metadata_path)
                if not isinstance(metadata, dict):
                    raise ValueError("Expected JSON object")
                required = {"arxiv_id", "title", "authors", "submission_date", "revised_dates", "venue", "versions"}
                missing = required - set(metadata)
                if missing:
                    raise ValueError(f"Missing fields: {sorted(missing)}")
                if metadata["arxiv_id"] != arxiv_id:
                    raise ValueError("arxiv_id mismatch")
                if not isinstance(metadata["title"], str) or not metadata["title"].strip():
                    raise ValueError("title must be nonempty string")
                if not isinstance(metadata["authors"], list) or not all(isinstance(a, str) and a.strip() for a in metadata["authors"]):
                    raise ValueError("authors must be nonempty strings in a list")
                if not iso_valid(metadata["submission_date"]):
                    raise ValueError("invalid submission_date")
                revised = metadata["revised_dates"]
                if not isinstance(revised, list) or not all(iso_valid(d) for d in revised):
                    raise ValueError("invalid revised_dates")
                if len(revised) != latest - 1:
                    raise ValueError(f"Expected {latest-1} revised dates, got {len(revised)}")
                if metadata["venue"] is not None and not isinstance(metadata["venue"], str):
                    raise ValueError("venue must be string or null")
                vs = metadata["versions"]
                if not isinstance(vs, list) or [v.get("version") if isinstance(v, dict) else None for v in vs] != expected:
                    raise ValueError("metadata.versions must cover all expected versions")
                add(arxiv_id, "", "metadata", "PASS")
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
                add(arxiv_id, "", "metadata", "FAIL", exc)

        # Version states from Member 2
        versions_path = paper_dir / "versions.json"
        versions = {}
        if versions_path.is_file():
            try:
                contents = load_json(versions_path)
                if not isinstance(contents, dict) or contents.get("arxiv_id") != arxiv_id or not isinstance(contents.get("versions"), dict):
                    raise ValueError("Invalid versions.json structure or arxiv_id")
                versions = contents["versions"]
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                add(arxiv_id, "", "versions_json", "FAIL", exc)
        else:
            add(arxiv_id, "", "versions_json", "PENDING", "versions.json not found")

        for version in expected:
            state = versions.get(version)
            if not isinstance(state, dict):
                add(arxiv_id, version, "version", "PENDING", "No final version record")
                continue
            status = state.get("status")
            if status not in KNOWN_SOURCE:
                add(arxiv_id, version, "version", "FAIL", f"Unknown status: {status}")
                continue
            if status in {"failed", "not_found"}:
                add(arxiv_id, version, "version", "PENDING", f"Source status: {status}; inspect/retry")
                continue
            folder = paper_dir / version
            if not folder.is_dir():
                add(arxiv_id, version, "source_dir", "FAIL", "Final source folder missing")
                continue
            files = source_files(folder)
            forbidden = [str(p.relative_to(folder)) for p in files if p.is_symlink() or p.suffix.lower() not in {".tex", ".bib"}]
            if forbidden:
                add(arxiv_id, version, "source_types", "FAIL", f"Unexpected files: {forbidden[:6]}")
            else:
                add(arxiv_id, version, "source_types", "PASS")
            if status == "success" and not files:
                add(arxiv_id, version, "source_count", "FAIL", "Success but no kept source files")
            if status in {"pdf_only", "no_tex"} and files:
                add(arxiv_id, version, "source_empty", "FAIL", f"{status} should have an empty directory")
            recorded = state.get("kept_files")
            if status == "success" and isinstance(recorded, list):
                absent = [f for f in recorded if not (folder / f).is_file()]
                if absent:
                    add(arxiv_id, version, "kept_files", "FAIL", f"Listed files missing: {absent[:6]}")
            if status == "success" and state.get("skipped"):
                add(arxiv_id, version, "skipped_files", "WARN", "Some archive members were skipped")
            if status in FINAL_SOURCE and not forbidden and not (status == "success" and not files):
                add(arxiv_id, version, "version", "PASS", status)

        # Semantic Scholar references from Member 3
        all_path = paper_dir / "references_all.json"
        selected_path = paper_dir / "references.json"
        if not all_path.is_file():
            add(arxiv_id, "", "references", "PENDING", "references_all.json missing")
            continue
        try:
            doc = load_json(all_path)
            if not isinstance(doc, dict) or doc.get("arxiv_id") != arxiv_id:
                raise ValueError("references_all.json must identify the citing arxiv_id")
            refs = doc.get("references")
            if not isinstance(refs, list):
                raise ValueError("references must be a list")
            ref_status = doc.get("status")
            if ref_status not in KNOWN_REF:
                raise ValueError(f"Unknown Semantic Scholar status: {ref_status}")
            if ref_status in {"unavailable", "not_found", "api_error"}:
                add(arxiv_id, "", "references_status", "WARN", ref_status)
            elif ref_status == "success" and not refs:
                add(arxiv_id, "", "references_status", "FAIL", "success but no references")
            elif ref_status == "empty" and refs:
                add(arxiv_id, "", "references_status", "FAIL", "empty but references exist")
            else:
                add(arxiv_id, "", "references_status", "PASS", ref_status)
            keys = set()
            for index, ref in enumerate(refs):
                if not isinstance(ref, dict):
                    add(arxiv_id, "", "reference_fields", "FAIL", f"Reference #{index} is not an object")
                    continue
                expected_fields = {"ref_arxiv_id", "ref_title", "ref_authors", "ref_submission_date", "ref_s2_id", "metadata_status"}
                missing = expected_fields - set(ref)
                if missing or not isinstance(ref.get("ref_authors"), list):
                    add(arxiv_id, "", "reference_fields", "FAIL", f"Reference #{index} invalid: {sorted(missing)}")
                ident = ref.get("ref_arxiv_id")
                if isinstance(ident, str) and ident:
                    keys.add(ident if "/" in ident else ident.replace(".", "-"))
                dt = ref.get("ref_submission_date")
                if dt is not None and not iso_valid(dt):
                    add(arxiv_id, "", "reference_date", "FAIL", f"Reference #{index} invalid submission date")
            if not selected_path.is_file():
                add(arxiv_id, "", "references_subset", "PENDING", "references.json missing")
            else:
                selected = load_json(selected_path)
                if not isinstance(selected, dict):
                    raise ValueError("references.json must be a dictionary")
                if set(selected) != keys:
                    add(arxiv_id, "", "references_subset", "FAIL",
                        f"Mismatch: expected {len(keys)} unique arXiv IDs, found {len(selected)}")
                else:
                    add(arxiv_id, "", "references_subset", "PASS")
            if doc.get("arxiv_dates_complete") is False:
                add(arxiv_id, "", "reference_dates", "WARN", "Submission-date lookup still pending")
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            add(arxiv_id, "", "references", "FAIL", exc)

    return issues


def main(argv=None):
    p = argparse.ArgumentParser(description="Validate the Group 11 Milestone 1 data directory")
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, help="CSV output (default: DATA_DIR/logs/validation_report.csv)")
    p.add_argument("--strict", action="store_true", help="Return exit code 1 when FAIL or PENDING exists")
    args = p.parse_args(argv)
    output = args.output or args.data_dir / "logs" / "validation_report.csv"
    issues = validate(args.data_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(issues)
    counts = Counter(row["status"] for row in issues)
    print(f"Wrote {len(issues)} checks to {output}")
    print(" ".join(f"{label}={counts[label]}" for label in ("PASS", "WARN", "PENDING", "FAIL")))
    return 1 if args.strict and (counts["FAIL"] or counts["PENDING"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
