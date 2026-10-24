"""Measure *standalone, fresh* end-to-end wall time for explicitly sampled arXiv IDs.

Runs main.py from discovery through references for EACH individual paper on its own
fresh output directory. Thus measured time includes batch/request overhead and is
NOT equivalent to latency within the bulk parallel pipeline. This deliberately
makes the measurement definition auditable rather than adding overlapping stage times.

Example (Colab CPU-only, only when authorised to make arXiv/S2 requests):
python src/per_paper_benchmark.py --output-root /content/drive/MyDrive/group11-data/paper_timing_samples \
    --ids 2511.21709 2511.21710 --interval 2 --disk-interval 300

Outputs after each paper: paper_timings.csv and per_paper_summary.json.
No data or secrets are pushed to GitHub.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from benchmark import run as benchmark_run
from validate import validate

ID_RE = re.compile(r"^\d{4}\.\d{4,5}$")
FINAL_SOURCE = {"success", "pdf_only", "no_tex"}
FINAL_REFS = {"success", "empty"}
COLUMNS = ["arxiv_id", "wall_seconds", "benchmark_return_code", "status", "source_complete",
           "reference_complete", "run_id", "benchmark_summary", "completed_utc"]


def read_json(path: Path):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def evaluate_paper(output_dir: Path, arxiv_id: str, return_code: int) -> dict:
    """Check completeness, not merely whether main.py returned exit code 0."""
    paper_dir = output_dir / "corpus" / arxiv_id.replace(".", "-")
    metadata = read_json(paper_dir / "metadata.json")
    manifest = output_dir / "work" / "paper_manifest.jsonl"
    records = []
    if manifest.is_file():
        with manifest.open(encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                    if item.get("arxiv_id") == arxiv_id:
                        records.append(item)
                except ValueError:
                    continue
    rec = records[-1] if records else {}
    source = read_json(paper_dir / "versions.json") or {}
    vstates = source.get("versions", {}) if isinstance(source.get("versions"), dict) else {}
    expected = rec.get("versions", [])
    source_ok = bool(expected and isinstance(metadata, dict) and
                     all(isinstance(vstates.get(v), dict) and
                         vstates[v].get("status") in FINAL_SOURCE and
                         (paper_dir / v).is_dir() for v in expected))
    refs = read_json(paper_dir / "references_all.json")
    # In Member 3's revised references.py, a completed Semantic Scholar fetch
    # can still have arXiv citation dates outstanding. Do not label a paper's
    # entire requested full pipeline complete until DateWorker finished those.
    refs_ok = bool(isinstance(refs, dict) and refs.get("status") in FINAL_REFS
                   and refs.get("arxiv_dates_complete") is True)
    quality_ok = not any(row["status"] in {"FAIL", "PENDING"}
                         for row in validate(output_dir, progress_every=0))
    if return_code != 0:
        state = "process_error"
    elif source_ok and refs_ok and quality_ok:
        state = "complete"
    else:
        state = "incomplete"
    return {"status": state, "source_complete": source_ok,
            "reference_complete": refs_ok}


def timing_summary(rows: list[dict]):
    complete = [float(row["wall_seconds"]) for row in rows
                if row["status"] == "complete" and row["wall_seconds"] not in (None, "")]
    return {
        "measurement": "fresh standalone full-pipeline end-to-end wall seconds for sampled papers",
        "sample_attempted": len(rows),
        "sample_complete": len(complete),
        "sample_incomplete_or_failed": len(rows) - len(complete),
        "mean_seconds_per_complete_sample_paper": sum(complete) / len(complete) if complete else None,
        "min_seconds_per_complete_sample_paper": min(complete) if complete else None,
        "max_seconds_per_complete_sample_paper": max(complete) if complete else None,
        "caveat": ("These measurements run each paper independently and cannot be presented as "
                   "the observed per-paper latency of a batch crawler; request/batch overhead differs. "
                   "Missing Semantic Scholar data are not treated as successful. The observed "
                   "sample average is NOT automatically the mean of all assigned papers."),
    }


def write_reports(root: Path, rows: list[dict]):
    out = root / "paper_timings.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    (root / "per_paper_summary.json").write_text(
        json.dumps(timing_summary(rows), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fresh end-to-end paper timing on Colab CPU-only")
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--ids", nargs="+", required=True)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--disk-interval", type=float, default=300)
    args = parser.parse_args(argv)
    if not all(ID_RE.fullmatch(id_) for id_ in args.ids):
        parser.error("Every arXiv ID must have the form YYMM.NNNN[N]")
    if len(set(args.ids)) != len(args.ids):
        parser.error("Do not repeat the same paper ID")
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "paper_timings.csv").exists():
        parser.error("Output already has paper_timings.csv; use a fresh --output-root for a new experiment")
    for arxiv_id in args.ids:
        paper_output = root / arxiv_id.replace(".", "-")
        if paper_output.exists() and any(paper_output.iterdir()):
            parser.error(f"Output is not fresh: {paper_output}. Never mix resume with fresh timing")

    rows = []
    for arxiv_id in args.ids:
        target = root / arxiv_id.replace(".", "-")
        target.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(Path(__file__).with_name("main.py")),
                   "--start", arxiv_id, "--end", arxiv_id, "--data-dir", str(target)]
        print(f"=== Fresh standalone full-pipeline timing: {arxiv_id} ===", flush=True)
        rc = benchmark_run(command, target, target / "logs", args.interval, args.disk_interval)
        summaries = sorted((target / "logs").glob("performance_summary_*.json"))
        info = read_json(summaries[-1]) if summaries else None
        state = evaluate_paper(target, arxiv_id, rc)
        rows.append({"arxiv_id": arxiv_id,
                     "wall_seconds": info.get("wall_seconds") if isinstance(info, dict) else "",
                     "benchmark_return_code": rc, "status": state["status"],
                     "source_complete": state["source_complete"],
                     "reference_complete": state["reference_complete"],
                     "run_id": info.get("run_id") if isinstance(info, dict) else "",
                     "benchmark_summary": str(summaries[-1]) if summaries else "",
                     "completed_utc": datetime.now(timezone.utc).isoformat()})
        write_reports(root, rows)
        print(f"{arxiv_id}: {state['status']}; saved {root / 'paper_timings.csv'}", flush=True)
    return 0 if all(row["status"] == "complete" for row in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
