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
import hashlib
import json
import time
import re
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath

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


def read_ids_file(path: Path) -> set[str]:
    """Expected ID list from the official assignment, one ID per line (header permitted)."""
    ids = set()
    with path.open(encoding="utf-8-sig") as stream:
        for lineno, line in enumerate(stream, 1):
            value = line.strip().split(",", 1)[0].strip()
            if not value or value.lower() == "arxiv_id":
                continue
            if not ID_RE.fullmatch(value):
                raise ValueError(f"Invalid assigned ID on line {lineno}: {value!r}")
            ids.add(value)
    return ids


def read_verified_404(path: Path) -> set[tuple[str, str]]:
    """Review-only CSV: arxiv_id,version. Verification must be documented externally."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"arxiv_id", "version"}.issubset(set(reader.fieldnames or [])):
            raise ValueError("CSV requires arxiv_id,version headers")
        return {(r["arxiv_id"].strip(), r["version"].strip()) for r in reader}


def validate(data_dir: Path, expected_ids: set[str] | None = None,
             verified_404: set[tuple[str, str]] | None = None,
             finished: bool = False, verify_hashes: bool = False,
             progress_every: int = 0, max_papers: int | None = None):
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

    if expected_ids is not None:
        for arxiv_id in sorted(expected_ids - set(manifest)):
            add(arxiv_id, "", "assigned_id_missing", "FAIL", "Officially assigned ID absent from manifest")
        for arxiv_id in sorted(set(manifest) - expected_ids):
            add(arxiv_id, "", "unexpected_id", "WARN", "ID is not in the supplied assignment list")

    # .part files live in work/downloads and temporary extraction directories
    # under paper folders, not inside final v1/v2 directories.
    part_dir = data_dir / "work" / "downloads"
    if finished and part_dir.is_dir():
        for partial in part_dir.glob("*.part"):
            add("", "", "leftover_part", "WARN", str(partial.relative_to(data_dir)))

    scan_started = time.perf_counter()
    items = sorted(manifest.items())
    if max_papers is not None:
        if max_papers <= 0:
            raise ValueError("max_papers must be positive")
        items = items[:max_papers]
    total = len(items)
    for index, (arxiv_id, rec) in enumerate(items, start=1):
        if progress_every and (index == 1 or index % progress_every == 0 or index == total):
            print(f"[validate] checking paper {index}/{total}; elapsed={time.perf_counter()-scan_started:.1f}s; "
                  f"checks={len(issues)}", flush=True)
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

        if finished:
            for tmp in paper_dir.glob(".v*.tmp"):
                add(arxiv_id, "", "leftover_tmp", "WARN", str(tmp.relative_to(data_dir)))

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
                if status == "not_found" and (arxiv_id, version) in (verified_404 or set()):
                    add(arxiv_id, version, "version", "WARN", "404 reviewed as documented source exception")
                else:
                    add(arxiv_id, version, "version", "PENDING", f"Source status: {status}; investigate")
                continue
            folder = paper_dir / version
            if not folder.is_dir():
                add(arxiv_id, version, "source_dir", "FAIL", "Final source folder missing")
                continue
            files = source_files(folder)
            invalid_version = False
            forbidden = [str(p.relative_to(folder)) for p in files if p.is_symlink() or p.suffix.lower() not in {".tex", ".bib"}]
            if forbidden:
                invalid_version = True
                add(arxiv_id, version, "source_types", "FAIL", f"Unexpected files: {forbidden[:6]}")
            else:
                add(arxiv_id, version, "source_types", "PASS")
            if status == "success" and not files:
                invalid_version = True
                add(arxiv_id, version, "source_count", "FAIL", "Success but no kept source files")
            if status in {"pdf_only", "no_tex"} and files:
                invalid_version = True
                add(arxiv_id, version, "source_empty", "FAIL", f"{status} should have an empty directory")
            if status == "success":
                zero_bytes = [str(f.relative_to(folder)) for f in files if f.is_file() and f.stat().st_size == 0]
                if zero_bytes:
                    invalid_version = True
                    add(arxiv_id, version, "zero_byte_source", "FAIL", f"Empty files: {zero_bytes[:6]}")
            recorded = state.get("kept_files")
            if status == "success":
                if not isinstance(recorded, list):
                    invalid_version = True
                    add(arxiv_id, version, "kept_files", "FAIL", "kept_files missing or not a list")
                else:
                    # Only accept ordinary relative POSIX paths inside the version folder.
                    unsafe = [str(name) for name in recorded if
                              not isinstance(name, str) or not name or
                              PurePosixPath(name).is_absolute() or
                              any(part in (".", "..") for part in PurePosixPath(name).parts) or
                              "\\" in name or PurePosixPath(name).suffix.lower() not in (".tex", ".bib")]
                    if unsafe:
                        invalid_version = True
                        add(arxiv_id, version, "kept_paths", "FAIL", f"Invalid recorded paths: {unsafe[:6]}")
                    elif len(set(recorded)) != len(recorded):
                        invalid_version = True
                        add(arxiv_id, version, "kept_duplicates", "FAIL", "Duplicate paths in kept_files")
                    else:
                        actual = {f.relative_to(folder).as_posix() for f in files if f.is_file() and not f.is_symlink()}
                        missing = sorted(set(recorded) - actual)
                        extras = sorted(actual - set(recorded))
                        if missing:
                            invalid_version = True
                            add(arxiv_id, version, "kept_files", "FAIL", f"Listed files missing: {missing[:6]}")
                        if extras:
                            invalid_version = True
                            add(arxiv_id, version, "kept_extra", "FAIL", f"Files not in kept_files: {extras[:6]}")
                        if not missing and not extras:
                            actual_bytes = sum((folder / name).stat().st_size for name in recorded)
                            expected_bytes = state.get("kept_bytes")
                            if not isinstance(expected_bytes, int) or actual_bytes != expected_bytes:
                                invalid_version = True
                                add(arxiv_id, version, "kept_byte_count", "FAIL",
                                    f"Expected {expected_bytes} bytes but recorded files total {actual_bytes}")
                # A file size check does not prove bytes match the original archive.
                if verify_hashes:
                    known_hashes = state.get("sha256_by_file")
                    if not isinstance(known_hashes, dict):
                        add(arxiv_id, version, "source_hash", "WARN", "No archive-time SHA256 hashes; provenance unavailable")
                    else:
                        for name, digest in known_hashes.items():
                            file = folder / name
                            if file.is_file():
                                hasher = hashlib.sha256()
                                with file.open("rb") as handle:
                                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                                        hasher.update(chunk)
                                if hasher.hexdigest().lower() != str(digest).lower():
                                    invalid_version = True
                                    add(arxiv_id, version, "source_hash", "FAIL", f"Hash mismatch: {name}")
            if status == "success" and state.get("skipped"):
                add(arxiv_id, version, "skipped_files", "WARN", "Some archive members were skipped")
            if status in FINAL_SOURCE and not invalid_version:
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
            # Member 3 writes references.json from the FIRST occurrence of each
            # arXiv cited ID, and updates both files when arXiv dates arrive.
            # Comparing only keys misses stale titles/dates after an interrupted write.
            expected_subset = {}
            for ref in refs:
                if not isinstance(ref, dict) or not isinstance(ref.get("ref_arxiv_id"), str):
                    continue
                cited_id = ref["ref_arxiv_id"]
                if not cited_id:
                    continue
                cited_key = cited_id if "/" in cited_id else cited_id.replace(".", "-")
                expected_subset.setdefault(cited_key, {
                    "title": ref.get("ref_title"),
                    "authors": ref.get("ref_authors"),
                    "submission_date": ref.get("ref_submission_date"),
                    "semantic_scholar_id": ref.get("ref_s2_id"),
                })
            if not selected_path.is_file():
                add(arxiv_id, "", "references_subset", "PENDING", "references.json missing")
            else:
                selected = load_json(selected_path)
                if not isinstance(selected, dict):
                    raise ValueError("references.json must be a dictionary")
                if set(selected) != keys:
                    add(arxiv_id, "", "references_subset", "FAIL",
                        f"Mismatch: expected {len(keys)} unique arXiv IDs, found {len(selected)}")
                elif selected != expected_subset:
                    add(arxiv_id, "", "references_subset_content", "FAIL",
                        "references.json values differ from references_all.json (possible stale date/title/authors/ID)")
                else:
                    add(arxiv_id, "", "references_subset", "PASS")
            date_flag = doc.get("arxiv_dates_complete")
            if date_flag is False:
                add(arxiv_id, "", "reference_dates", "WARN", "Submission-date lookup still pending")
            elif not isinstance(date_flag, bool):
                add(arxiv_id, "", "reference_dates", "WARN",
                    "arxiv_dates_complete flag missing/invalid; date completion cannot be verified")
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            add(arxiv_id, "", "references", "FAIL", exc)

    return issues


def main(argv=None):
    p = argparse.ArgumentParser(description="Validate the Group 11 Milestone 1 data directory")
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, help="CSV output (default: DATA_DIR/logs/validation_report.csv)")
    p.add_argument("--strict", action="store_true", help="Return exit code 1 when FAIL or PENDING exists")
    p.add_argument("--assigned-ids", type=Path,
                   help="Official list of expected arXiv IDs, one per line (recommended for cross-month ranges)")
    p.add_argument("--expected-count", type=int,
                   help="Sanity check only; a matching count cannot prove each assigned ID is present")
    p.add_argument("--verified-404", type=Path,
                   help="Reviewed CSV with arxiv_id,version; only these 404 cases become WARN")
    p.add_argument("--finished", action="store_true", help="Flag leftover .part and .v*.tmp after crawler stops")
    p.add_argument("--verify-hashes", action="store_true", help="Check saved per-file SHA256 if available")
    p.add_argument("--progress-every", type=int, default=100, help="Print progress every N papers; 0 disables it")
    p.add_argument("--max-papers", type=int, help="DEV TEST ONLY: validate first N manifest papers; use --output outside full logs")
    args = p.parse_args(argv)
    output = args.output or args.data_dir / "logs" / "validation_report.csv"
    if args.max_papers is not None and (args.expected_count is not None or args.assigned_ids or args.strict):
        p.error("--max-papers is only a dev smoke test; do not combine with --expected-count, --assigned-ids, --strict")
    expected_ids = read_ids_file(args.assigned_ids) if args.assigned_ids else None
    verified = read_verified_404(args.verified_404) if args.verified_404 else None
    issues = validate(args.data_dir, expected_ids=expected_ids,
                      verified_404=verified, finished=args.finished,
                      verify_hashes=args.verify_hashes,
                      progress_every=args.progress_every, max_papers=args.max_papers)
    if args.expected_count is not None:
        manifest, _ = load_manifest(args.data_dir / "work" / "paper_manifest.jsonl")
        if len(manifest) != args.expected_count:
            issues.append(dict(arxiv_id="", version="", check_name="manifest_count", status="FAIL",
                               error_message=f"Expected {args.expected_count} IDs, found {len(manifest)}"))
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
