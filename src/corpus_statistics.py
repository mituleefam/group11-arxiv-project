"""Role 4: reproducible Milestone 1 statistics from current corpus states.

Usage:
  python src/corpus_statistics.py --data-dir /path/to/data
  python src/corpus_statistics.py --data-dir /path/to/data --benchmark-summary /path/to/summary.json

Important: append-only CSV logs contain retries, so this program primarily reads
manifest/versions.json/references_all.json (the latest saved states), not raw
counts from every log row. It never makes HTTP/API calls.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path, PurePosixPath

from validate import load_manifest, read_ids_file

FINAL_SOURCE = {"success", "pdf_only", "no_tex"}
REF_OK = {"success", "empty"}


def read_json(path: Path):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def mean(values):
    return sum(values) / len(values)


def analyze(root: Path, benchmark_summary: Path | None = None, assigned_ids: set[str] | None = None,
            paper_timings: Path | None = None):
    """Return documented metrics as a list of metric/value/unit/description dicts."""
    manifest, problems = load_manifest(root / "work" / "paper_manifest.jsonl")
    existing = [r for r in manifest.values() if r.get("status") == "success"]
    discovery = Counter(r.get("status", "unknown") for r in manifest.values())
    download_status = Counter()
    reference_status = Counter()
    n_versions = n_final_versions = n_success_versions = 0
    papers_source_complete = papers_pipeline_complete = papers_pipeline_core_complete = 0
    papers_with_metadata = 0
    paper_original, paper_kept = [], []
    summed_original = summed_kept = summed_images = 0
    n_ref_total = n_ref_with_arxiv = n_ref_complete = n_ref_incomplete = n_ref_unresolved = 0
    n_ref_papers = n_ref_succeeded = n_ref_dates_pending = n_ref_dates_unknown = 0
    source_seconds_per_paper = []
    n_invalid_source_versions = 0

    for rec in existing:
        arxiv_id = rec["arxiv_id"]
        paper = root / "corpus" / arxiv_id.replace(".", "-")
        m = read_json(paper / "metadata.json")
        metadata_ok = isinstance(m, dict) and m.get("arxiv_id") == arxiv_id
        papers_with_metadata += int(metadata_ok)
        expected = rec.get("versions") or []
        n_versions += len(expected)
        version_json = read_json(paper / "versions.json") or {}
        version_states = version_json.get("versions", {}) if isinstance(version_json, dict) else {}
        if not isinstance(version_states, dict):
            version_states = {}
        per_original = per_kept = per_images = 0
        per_seconds = 0.0
        all_final = True
        for version in expected:
            v = version_states.get(version) or {}
            status = v.get("status", "missing")
            download_status[status] += 1
            folder_exists = (paper / version).is_dir()
            # A success record with missing or empty kept files is not truly complete.
            valid_files = True
            if status == "success" and folder_exists:
                names = v.get("kept_files")
                if not isinstance(names, list) or not names:
                    valid_files = False
                else:
                    try:
                        safe = all(isinstance(name, str) and name and
                                   not PurePosixPath(name).is_absolute() and
                                   "\\" not in name and
                                   not any(part in (".", "..") for part in PurePosixPath(name).parts) and
                                   PurePosixPath(name).suffix.lower() in (".tex", ".bib")
                                   for name in names)
                        if safe and len(set(names)) == len(names):
                            kept = [paper / version / name for name in names]
                            actual = {p.relative_to(paper / version).as_posix()
                                      for p in (paper / version).rglob("*") if p.is_file() and not p.is_symlink()}
                            valid_files = (actual == set(names) and all(f.is_file() and not f.is_symlink()
                                           and f.stat().st_size > 0 for f in kept)
                                           and isinstance(v.get("kept_bytes"), int)
                                           and sum(f.stat().st_size for f in kept) == v["kept_bytes"])
                        else:
                            valid_files = False
                    except (OSError, ValueError):
                        valid_files = False
                n_invalid_source_versions += int(not valid_files)
            final = status in FINAL_SOURCE and folder_exists and valid_files
            n_final_versions += int(final)
            n_success_versions += int(status == "success" and final)
            all_final = all_final and final
            if final:
                per_original += int(v.get("original_bytes") or 0)
                per_kept += int(v.get("kept_bytes") or 0)
                per_images += int(v.get("image_bytes") or 0)
                per_seconds += float(v.get("total_seconds") or 0.0)
        if all_final and expected:
            papers_source_complete += 1
            paper_original.append(per_original)
            paper_kept.append(per_kept)
            source_seconds_per_paper.append(per_seconds)
        summed_original += per_original
        summed_kept += per_kept
        summed_images += per_images

        doc = read_json(paper / "references_all.json")
        ref_status = doc.get("status", "missing") if isinstance(doc, dict) else "missing"
        reference_status[ref_status] += 1
        ref_items = doc.get("references") if isinstance(doc, dict) else None
        # A status alone is not enough: ensure the saved reference document is consistent.
        reference_ok = (isinstance(doc, dict) and doc.get("arxiv_id") == arxiv_id and
                        isinstance(ref_items, list) and
                        ((ref_status == "empty" and not ref_items) or
                         (ref_status == "success" and bool(ref_items))))
        pipeline_core_ok = all_final and bool(expected) and metadata_ok and reference_ok
        papers_pipeline_core_complete += int(pipeline_core_ok)
        # Member 3 asynchronously resolves arXiv v1 dates for cited papers.
        # Fetching S2 references is not the same as completing the entire
        # requested reference metadata, so keep both metrics explicitly.
        paper_dates_complete = isinstance(doc, dict) and doc.get("arxiv_dates_complete") is True
        papers_pipeline_complete += int(pipeline_core_ok and paper_dates_complete)
        if reference_ok:
            refs = doc["references"]
            n_ref_papers += 1
            n_ref_total += len(refs)
            n_ref_with_arxiv += sum(bool(isinstance(r, dict) and r.get("ref_arxiv_id")) for r in refs)
            n_ref_complete += sum(isinstance(r, dict) and r.get("metadata_status") == "complete" for r in refs)
            n_ref_incomplete += sum(isinstance(r, dict) and r.get("metadata_status") == "incomplete" for r in refs)
            n_ref_unresolved += sum(isinstance(r, dict) and r.get("metadata_status") == "unresolved" for r in refs)
            n_ref_succeeded += int(reference_ok)
            n_ref_dates_pending += int(doc.get("arxiv_dates_complete") is False)
            n_ref_dates_unknown += int(not isinstance(doc.get("arxiv_dates_complete"), bool))

    rows = []
    def add(metric, value, unit, description):
        rows.append(dict(metric=metric, value=value if value is not None else "", unit=unit, description=description))

    add("assigned_ids_discovered", len(manifest), "ids", "Number of distinct IDs in manifest; NOT necessarily all assigned IDs")
    add("assigned_ids_expected", len(assigned_ids) if assigned_ids is not None else None, "ids", "From provided official ID list only")
    add("assigned_ids_missing_from_manifest", len(assigned_ids - set(manifest)) if assigned_ids is not None else None,
        "ids", "Number of official assigned IDs missing in manifest")
    add("source_versions_with_invalid_kept_files", n_invalid_source_versions, "versions", "Source success records with missing/zero-byte/mismatched source files")
    add("discovery_success_papers", len(existing), "papers", "Manifest papers marked success")
    add("discovery_not_found", discovery["not_found"], "ids", "Manifest latest records marked not_found")
    add("discovery_api_error", discovery["api_error"], "ids", "Manifest latest records marked api_error")
    add("metadata_files_present", papers_with_metadata, "papers", "Papers having matching metadata.json (not full schema validation)")
    add("versions_expected", n_versions, "versions", "Sum of version lists from successful discovery records")
    add("versions_final_record_and_folder", n_final_versions, "versions", "Source statuses success/pdf_only/no_tex with version directory present")
    add("versions_tex_success", n_success_versions, "versions", "Source versions marked success and directory present")
    add("version_handling_coverage", n_final_versions/n_versions if n_versions else None, "fraction", "Final source states / expected versions; includes legitimate no_tex/pdf_only")
    add("papers_source_complete", papers_source_complete, "papers", "Every listed source version has final source state and folder")
    add("papers_pipeline_core_complete", papers_pipeline_core_complete, "papers",
        "Source complete, metadata present, S2 reference success/empty; arXiv citation dates may remain pending")
    add("papers_pipeline_complete_strict", papers_pipeline_complete, "papers",
        "Core complete AND arxiv_dates_complete=true (new Member 3 reference schema)")
    add("pipeline_complete_rate_among_found", papers_pipeline_complete/len(existing) if existing else None, "fraction", "Operational completeness (source+metadata+S2 statuses), not a substitute for all validate.py checks")
    add("mean_uncompressed_original_bytes_per_source_complete_paper", mean(paper_original) if paper_original else None, "bytes/paper", "Mean total uncompressed archive size per fully source-processed paper")
    add("mean_kept_bytes_per_source_complete_paper", mean(paper_kept) if paper_kept else None, "bytes/paper", "Mean total kept .tex/.bib bytes per fully source-processed paper")
    add("total_original_uncompressed_bytes_recorded", summed_original, "bytes", "Total across versions with final status")
    add("total_image_bytes_recorded", summed_images, "bytes", "Image file bytes listed by extractor, not all removed files")
    add("total_kept_bytes_recorded", summed_kept, "bytes", "Total .tex/.bib bytes for versions with final status")
    add("image_bytes_share_original", summed_images/summed_original if summed_original else None, "fraction", "Image bytes / uncompressed original bytes")
    add("all_excluded_bytes_share_original", 1-summed_kept/summed_original if summed_original else None, "fraction", "All dropped bytes incl. figures and other files / uncompressed originals")
    add("mean_source_download_seconds_per_source_complete_paper", mean(source_seconds_per_paper) if source_seconds_per_paper else None,
        "seconds/paper", "Sum of per-version processing times per complete paper; NOT end-to-end paper latency")
    add("references_papers_with_doc", n_ref_papers, "papers", "Papers with S2 success/empty and valid references_all.json list")
    add("references_papers_success_or_empty", n_ref_succeeded, "papers", "S2 success/empty statuses")
    add("total_references_raw", n_ref_total, "references", "Total references from valid references_all.json")
    add("mean_references_per_references_document", n_ref_total/n_ref_papers if n_ref_papers else None,
        "references/paper", "Conditional mean: papers with S2 success/empty and valid reference documents, including genuine empty")
    add("references_per_found_paper_observed_lower_bound", n_ref_total/len(existing) if existing else None,
        "observed references/paper", "Observed references divided by all discovered papers: LOWER BOUND when docs are missing")
    add("references_per_assigned_id_observed_lower_bound", n_ref_total/len(assigned_ids) if assigned_ids else None,
        "observed references/id", "Only meaningful when official assigned-IDs list is supplied; LOWER BOUND")
    add("reference_collection_success_rate_found", n_ref_succeeded/len(existing) if existing else None,
        "fraction", "S2 success or genuine empty / all discovered papers, not the per-reference metadata completeness rate")
    add("references_with_arxiv_id", n_ref_with_arxiv, "references", "Count raw refs carrying arXiv ID")
    add("references_with_arxiv_id_rate", n_ref_with_arxiv/n_ref_total if n_ref_total else None, "fraction", "Refs with arXiv ID / all raw references")
    add("reference_metadata_complete_rate", n_ref_complete/n_ref_total if n_ref_total else None, "fraction",
        "Complete = S2 paperId, title and authors present / all raw references")
    add("reference_metadata_complete_count", n_ref_complete, "references", "S2 metadata_status complete")
    add("reference_metadata_incomplete_count", n_ref_incomplete, "references", "S2 metadata_status incomplete")
    add("reference_metadata_unresolved_count", n_ref_unresolved, "references", "S2 metadata_status unresolved")
    add("reference_papers_missing_arxiv_dates", n_ref_dates_pending, "papers", "Papers whose arXiv citation date lookup incomplete")
    add("reference_papers_unknown_arxiv_date_state", n_ref_dates_unknown, "papers",
        "S2 reference documents missing a valid arxiv_dates_complete flag; cannot confirm date completion")
    for label in sorted(download_status):
        add(f"source_version_status_{label}", download_status[label], "versions", "Final status from versions.json or missing")
    for label in sorted(reference_status):
        add(f"reference_paper_status_{label}", reference_status[label], "papers", "Latest status from references_all.json or missing")
    if problems:
        add("invalid_manifest_lines", len(problems), "lines", "Malformed JSONL lines ignored by manifest parser")

    run_log = root / "logs" / "run_log.csv"
    discovery_seconds = 0.0
    discovery_runs = set()
    if run_log.is_file():
        with run_log.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("stage") == "discovery":
                    try:
                        seconds = float(row.get("seconds", ""))
                    except (ValueError, TypeError):
                        continue
                    discovery_seconds += seconds
                    discovery_runs.add(row.get("run_id", "unknown"))
    add("discovery_total_logged_stage_seconds", round(discovery_seconds, 3) if discovery_runs else None,
        "seconds", "Sum of discovery-stage wall times across logged runs (including retries/resumes); not full-pipeline wall time")
    add("discovery_logged_runs", len(discovery_runs), "runs", "Number of distinct run_id values with discovery records")
    if benchmark_summary:
        benchmark = read_json(benchmark_summary)
        if not isinstance(benchmark, dict):
            raise ValueError(f"Invalid benchmark summary: {benchmark_summary}")
        for key, unit in [("wall_seconds", "seconds"), ("ram_mean_bytes", "bytes"),
                          ("ram_sampled_peak_bytes", "bytes"), ("data_dir_sampled_peak_bytes", "bytes"),
                          ("data_dir_final_bytes", "bytes")]:
            add("benchmark_" + key, benchmark.get(key), unit,
                "Measured during the specified benchmark invocation; specify its run/config in report")
        wall = benchmark.get("wall_seconds")
        fresh = benchmark.get("started_with_fresh_data_dir") is True
        add("benchmark_normalized_wall_seconds_per_found_paper",
            wall/len(existing) if fresh and isinstance(wall, (int, float)) and existing else None,
            "seconds/paper", "Fresh end-to-end wall / found paper count only; throughput-normalized, NOT per-paper latency. None for resume/unknown provenance.")
        add("benchmark_normalized_wall_seconds_per_assigned_id",
            wall/len(assigned_ids) if fresh and isinstance(wall, (int, float)) and assigned_ids else None,
            "seconds/id", "Fresh wall/number of officially assigned IDs; valid only if corresponding scope and completed pipeline agree.")
    if paper_timings is not None:
        # Per-paper results are separately measured *standalone fresh samples*.
        # They must never be conflated with average latency in batch mode.
        from per_paper_benchmark import timing_summary
        try:
            with paper_timings.open(encoding="utf-8-sig", newline="") as source:
                timing_rows = list(csv.DictReader(source))
        except OSError as exc:
            raise ValueError(f"Cannot read per-paper sample: {paper_timings}: {exc}") from exc
        measured = timing_summary(timing_rows)
        add("standalone_paper_sample_count", measured["sample_attempted"], "papers",
            "Number of individually run fresh full-pipeline timing samples")
        add("standalone_paper_complete_sample_count", measured["sample_complete"], "papers",
            "Only papers with validated source and references; S2 HTTP 429 is excluded")
        add("standalone_paper_sample_mean_seconds", measured["mean_seconds_per_complete_sample_paper"],
            "seconds/paper", "Conditional sample mean for standalone fresh per-paper full pipeline. NOT bulk latency or assigned-population mean.")
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description="Compute Milestone 1 statistics from local corpus and logs")
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--benchmark-summary", type=Path, help="Optional selected full benchmark summary JSON")
    p.add_argument("--assigned-ids", type=Path, help="One ID per line from the official assignment")
    p.add_argument("--paper-timings", type=Path, help="CSV of separately measured fresh standalone per-paper samples")
    args = p.parse_args(argv)
    output = args.output or args.data_dir / "logs" / "summary_statistics.csv"
    ids = read_ids_file(args.assigned_ids) if args.assigned_ids else None
    rows = analyze(args.data_dir, args.benchmark_summary, assigned_ids=ids,
                   paper_timings=args.paper_timings)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "value", "unit", "description"])
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} metrics to {output}")
    for row in rows[:12]:
        print(f"  {row['metric']}: {row['value']} {row['unit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
