"""Export the small tables the Milestone 1 Jupyter Book is built from.

The book is built by GitHub Actions, which cannot read the members' data
folders on Google Drive, and the raw corpus must not be published before
Milestone 3. This tool reads every member's --data-dir and writes a few
aggregated tables (counts, sizes, statuses, timings; no LaTeX text, no
reference titles) into the book repository. It sends no request and changes
nothing in the data folders.

Usage (each argument is a member's --data-dir, optionally named NAME=PATH):

    python src/export_report_data.py \
        m1=/content/drive/MyDrive/group11-data/02_trial/AQuan_2605-12338_2605-17337 \
        m2=... m3=... m4=... m5=... \
        --out ../mst1-book/data/m1

Outputs in --out:
    meta.json               export time, code commit, members, warnings
    members.csv             group_benchmark.py's row per member, then "group"
    statistics.csv          corpus_statistics.py's metrics, one block per member
    papers.csv.gz           one row per assigned ID: discovery, metadata, references
    versions.csv.gz         one row per expected version: final state from versions.json
    run_log.csv             every stage of every run (logs/run_log.csv)
    benchmark_runs.csv      every benchmark.py invocation (benchmark_runs.aggregate)
    performance_samples.csv.gz  RAM/disk samples, thinned to --max-samples per run
    validation_counts.csv   validation_report.csv counted by check and status
    validation_issues.csv   the non-PASS rows, at most --max-issues per member

Run validate.py --finished on each folder first: its report
(logs/validation_report.csv) is copied, not recomputed. The corpus
statistics are recomputed here with corpus_statistics.analyze(), so they
always match the corpus on disk at export time.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import subprocess
from collections import Counter
from pathlib import Path

from benchmark_runs import aggregate
from common import DataLayout, folder_name, now_iso
from corpus_statistics import analyze
from group_benchmark import COLUMNS, group_row, member_row
from validate import load_manifest

PAPER_FIELDS = ["member", "arxiv_id", "discovery_status", "n_versions", "has_metadata",
                "primary_category", "submission_date", "n_revisions", "has_venue",
                "ref_status", "s2_reference_count", "n_references", "n_with_arxiv_id",
                "n_complete", "n_incomplete", "n_unresolved", "n_with_ref_date",
                "arxiv_dates_complete"]
VERSION_FIELDS = ["member", "arxiv_id", "version", "status", "source_format", "downloaded_bytes",
                  "original_bytes", "image_bytes", "kept_bytes", "kept_files", "tex_files",
                  "bib_files", "attempts", "total_seconds"]
SAMPLE_FIELDS = ["member", "run_id", "elapsed_seconds", "rss_tree_bytes", "process_count",
                 "data_dir_bytes", "colab_disk_used_bytes"]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def paper_rows(name: str, layout: DataLayout) -> list[dict]:
    """One row per ID of the manifest, with what each stage left on disk."""
    manifest, _ = load_manifest(layout.manifest)
    rows = []
    for arxiv_id, rec in sorted(manifest.items()):
        row = {"member": name, "arxiv_id": arxiv_id, "discovery_status": rec.get("status"),
               "n_versions": len(rec.get("versions") or [])}
        paper = layout.corpus / folder_name(arxiv_id)
        meta = read_json(paper / "metadata.json")
        if isinstance(meta, dict):
            row.update(has_metadata=True, primary_category=meta.get("primary_category"),
                       submission_date=meta.get("submission_date"),
                       n_revisions=len(meta.get("revised_dates") or []),
                       has_venue=bool(meta.get("venue")))
        else:
            row["has_metadata"] = False
        doc = read_json(paper / "references_all.json")
        if isinstance(doc, dict):
            refs = [r for r in doc.get("references") or [] if isinstance(r, dict)]
            quality = Counter(r.get("metadata_status") for r in refs)
            row.update(ref_status=doc.get("status"), s2_reference_count=doc.get("s2_reference_count"),
                       n_references=len(refs),
                       n_with_arxiv_id=sum(bool(r.get("ref_arxiv_id")) for r in refs),
                       n_complete=quality["complete"], n_incomplete=quality["incomplete"],
                       n_unresolved=quality["unresolved"],
                       n_with_ref_date=sum(bool(r.get("ref_arxiv_id") and r.get("ref_submission_date"))
                                           for r in refs),
                       arxiv_dates_complete=doc.get("arxiv_dates_complete"))
        else:
            # api_error writes no document: the references are unknown, not zero
            row["ref_status"] = "missing" if rec.get("status") == "success" else ""
        rows.append(row)
    return rows


def version_rows(name: str, layout: DataLayout) -> list[dict]:
    """Final state of every expected version, as versions.json records it."""
    manifest, _ = load_manifest(layout.manifest)
    rows = []
    for arxiv_id, rec in sorted(manifest.items()):
        if rec.get("status") != "success":
            continue
        states = (read_json(layout.corpus / folder_name(arxiv_id) / "versions.json") or {}).get("versions")
        states = states if isinstance(states, dict) else {}
        for version in rec.get("versions") or []:
            v = states.get(version) or {}
            kept = v.get("kept_files") if isinstance(v.get("kept_files"), list) else []
            rows.append({"member": name, "arxiv_id": arxiv_id, "version": version,
                         "status": v.get("status", "missing"), "source_format": v.get("source_format"),
                         "downloaded_bytes": v.get("downloaded_bytes"),
                         "original_bytes": v.get("original_bytes"), "image_bytes": v.get("image_bytes"),
                         "kept_bytes": v.get("kept_bytes"), "kept_files": len(kept),
                         "tex_files": sum(f.lower().endswith(".tex") for f in kept),
                         "bib_files": sum(f.lower().endswith(".bib") for f in kept),
                         "attempts": v.get("attempts_total", v.get("attempts")),
                         "total_seconds": v.get("total_seconds")})
    return rows


def sample_rows(name: str, logs: Path, limit: int) -> list[dict]:
    """benchmark.py samples of every invocation, keeping at most `limit` per run."""
    rows = []
    for path in sorted(logs.glob("performance_samples_*.csv")):
        run_id = path.stem.removeprefix("performance_samples_")
        samples = read_csv(path)
        step = max(1, -(-len(samples) // limit))  # ceil: keeps the first and evenly spaced rows
        for sample in samples[::step]:
            rows.append({"member": name, "run_id": run_id, **sample})
    return rows


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             cwd=Path(__file__).parent, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export aggregated tables for the Jupyter Book")
    parser.add_argument("folders", nargs="+", metavar="[NAME=]DATA_DIR")
    parser.add_argument("--out", type=Path, required=True, help="e.g. ../mst1-book/data/m1")
    parser.add_argument("--max-samples", type=int, default=2000, help="samples kept per benchmark run")
    parser.add_argument("--max-issues", type=int, default=500, help="non-PASS rows kept per member")
    args = parser.parse_args(argv)
    args.members = []
    for item in args.folders:
        name, _, path = item.partition("=") if "=" in item else ("", "", item)
        root = Path(path)
        if not (root / "work").is_dir():
            parser.error(f"{root} has no work/ folder: is it a --data-dir?")
        args.members.append((name or root.name, root))
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    members, stats, papers, versions, runs, samples = [], [], [], [], [], []
    stages, val_counts, val_issues = [], Counter(), []

    for name, root in args.members:
        layout = DataLayout(root)
        print(f"[{name}] reading {root}")
        row = member_row(name, root, warnings)
        row["data_dir"] = root.name  # the folder name only: no personal Drive path in the book
        members.append(row)
        stats += [{"member": name, **m} for m in analyze(root)]
        papers += paper_rows(name, layout)
        versions += version_rows(name, layout)
        stages += [{"member": name, **r} for r in read_csv(layout.logs / "run_log.csv")]
        member_runs, _ = aggregate(layout.logs)
        runs += [{"member": name, **r} for r in member_runs]
        samples += sample_rows(name, layout.logs, args.max_samples)
        report = read_csv(layout.logs / "validation_report.csv")
        if not report:
            warnings.append(f"{name}: no logs/validation_report.csv, run validate.py --finished first")
        for r in report:
            val_counts[(name, r.get("check_name"), r.get("status"))] += 1
        val_issues += [{"member": name, **r} for r in report if r.get("status") != "PASS"][:args.max_issues]

    members.append(group_row(members))
    write_csv(args.out / "members.csv", COLUMNS, members)
    write_csv(args.out / "statistics.csv", ["member", "metric", "value", "unit", "description"], stats)
    write_csv(args.out / "papers.csv.gz", PAPER_FIELDS, papers)
    write_csv(args.out / "versions.csv.gz", VERSION_FIELDS, versions)
    write_csv(args.out / "run_log.csv",
              ["member", "run_id", "stage", "started_at", "finished_at", "seconds", "items", "note"], stages)
    write_csv(args.out / "benchmark_runs.csv",
              ["member", "run_id", "return_code", "interrupted", "started_utc", "ended_utc",
               "wall_seconds", "ram_sampled_peak_bytes", "data_dir_sampled_peak_bytes",
               "data_dir_final_bytes", "sample_count"], runs)
    write_csv(args.out / "performance_samples.csv.gz", SAMPLE_FIELDS, samples)
    write_csv(args.out / "validation_counts.csv", ["member", "check_name", "status", "count"],
              [{"member": m, "check_name": c, "status": s, "count": n}
               for (m, c, s), n in sorted(val_counts.items())])
    write_csv(args.out / "validation_issues.csv",
              ["member", "arxiv_id", "version", "check_name", "status", "error_message"], val_issues)
    meta = {"generated_at": now_iso(), "code_commit": git_commit(),
            "members": [name for name, _ in args.members], "warnings": warnings}
    (args.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote {len(papers)} papers, {len(versions)} versions, {len(runs)} benchmark runs to {args.out}")
    for warning in warnings:
        print(f"WARNING {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
