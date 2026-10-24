"""Summarize multiple completed benchmark invocations without pretending they are one fresh crawl.

Reads performance_summary_*.json, optionally sample CSV files beside each summary.
Each resumed job remains a separately documented invocation.
No API calls, no corpus changes.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def aggregate(log_dir: Path):
    rows = []
    ram_sample_total = 0.0
    ram_sample_count = 0
    errors = []
    for path in sorted(log_dir.glob("performance_summary_*.json")):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            run_id = info.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("missing run_id")
            samples = log_dir / f"performance_samples_{run_id}.csv"
            count = 0
            if samples.exists():
                with samples.open(encoding="utf-8-sig", newline="") as f:
                    for sample in csv.DictReader(f):
                        value = sample.get("rss_tree_bytes", "")
                        if value and float(value) > 0 and int(sample.get("process_count") or 1) > 0:
                            ram_sample_total += float(value)
                            ram_sample_count += 1
                            count += 1
            rows.append({
                "run_id": run_id, "return_code": info.get("return_code"),
                "interrupted": info.get("interrupted"),
                "started_utc": info.get("started_utc"),
                "ended_utc": info.get("ended_utc"),
                "wall_seconds": info.get("wall_seconds"),
                "ram_sampled_peak_bytes": info.get("ram_sampled_peak_bytes"),
                "data_dir_sampled_peak_bytes": info.get("data_dir_sampled_peak_bytes"),
                "data_dir_final_bytes": info.get("data_dir_final_bytes"),
                "sample_count": count,
                "command": json.dumps(info.get("command", []), ensure_ascii=False),
            })
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{path.name}: {exc}")
    wall = [float(x["wall_seconds"]) for x in rows if isinstance(x["wall_seconds"], (int, float))]
    peaks = [float(x["ram_sampled_peak_bytes"]) for x in rows if isinstance(x["ram_sampled_peak_bytes"], (int, float))]
    disks = [float(x["data_dir_sampled_peak_bytes"]) for x in rows if isinstance(x["data_dir_sampled_peak_bytes"], (int, float))]
    # An interrupted Colab VM may leave only per-sample CSV/heartbeat JSON;
    # report these invocations explicitly, never fabricate their final wall time.
    documented = {r["run_id"] for r in rows}
    orphaned_progress = []
    for path in sorted(log_dir.glob("performance_progress_*.json")):
        if path.stem.removeprefix("performance_progress_") not in documented:
            try:
                progress = json.loads(path.read_text(encoding="utf-8"))
                orphaned_progress.append({
                    "run_id": progress.get("run_id"),
                    "last_observed_utc": progress.get("last_observed_utc"),
                    "observed_elapsed_seconds": progress.get("observed_elapsed_seconds"),
                    "note": "Incomplete session: no final benchmark summary; not included in wall sum",
                })
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"{path.name}: {exc}")
    summary = {
        "invocations": len(rows),
        "orphaned_incomplete_invocations": orphaned_progress,
        "orphaned_incomplete_invocation_count": len(orphaned_progress),
        "completed_with_exit_zero": sum(x["return_code"] == 0 and not x["interrupted"] for x in rows),
        "sum_measured_invocation_wall_seconds": sum(wall),
        "highest_sampled_process_rss_peak_bytes": max(peaks) if peaks else None,
        "highest_sampled_data_directory_bytes": max(disks) if disks else None,
        "mean_sampled_rss_bytes_across_available_rows": (ram_sample_total / ram_sample_count if ram_sample_count else None),
        "number_of_ram_samples": ram_sample_count,
        "errors": errors,
        "IMPORTANT": ("Summed invocation wall times include checkpoint/resume overhead. "
                      "This is NOT verified full fresh end-to-end wall time, nor wall time elapsed "
                      "between the first and last invocation. Missing/incomplete sessions without "
                      "summary are excluded. Overlapping runs can double-count time."),
    }
    return rows, summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--logs-dir", type=Path, required=True)
    args = p.parse_args()
    rows, summary = aggregate(args.logs_dir)
    output = args.logs_dir / "benchmark_runs_overview.csv"
    columns = ["run_id", "return_code", "interrupted", "started_utc", "ended_utc",
               "wall_seconds", "ram_sampled_peak_bytes", "data_dir_sampled_peak_bytes",
               "data_dir_final_bytes", "sample_count", "command"]
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    summary_path = args.logs_dir / "benchmark_runs_overview.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Benchmarked invocations: {len(rows)}")
    print(f"Measured invocation wall sum: {summary['sum_measured_invocation_wall_seconds']:.2f} seconds")
    print("WARNING: this is NOT a single fresh end-to-end wall time")
    print(f"Outputs: {output}, {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
