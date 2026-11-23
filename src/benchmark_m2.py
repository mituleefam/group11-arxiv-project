"""Member 4: M2-specific wrapper of M1's untouched benchmark.py.

    python src/benchmark_m2.py --data-dir data --workers 2 --limit 5 --force

Delegates RAM/disk/wall sampling to benchmark.py, runs pipeline_m2.py as a
child process, then appends a concise per-run record. Never counts the
sum of worker processing times as end-to-end wall time.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("run_id", "started_utc", "ended_utc", "status", "exit_code", "wall_seconds",
          "ram_sampled_peak_bytes", "ram_mean_bytes", "parquet_final_bytes",
          "data_dir_sampled_peak_bytes", "workers", "paper_limit", "force_reparse",
          "cpu_only_claim", "environment", "benchmark_summary", "notes")


def total_parquet_bytes(root: Path) -> int | None:
    if not root.is_dir():
        return None
    files = list(root.rglob("*.parquet"))
    return sum(file.stat().st_size for file in files) if files else None


def inspect_completion(data_dir: Path, limit: int) -> tuple[int, list[str]]:
    """Confirm every selected paper has a final successful parse record.

    pipeline_m2.py can exit 0 even when individual workers reported `failed`.
    Do not turn that process-level success into a false complete benchmark.
    """
    from checkpoint import ParseManifest
    from common import DataLayout
    from pipeline_m2 import list_papers

    layout = DataLayout(data_dir)
    selected = list_papers(layout)
    if limit:
        selected = selected[:limit]
    states = ParseManifest(layout.logs).load()
    unfinished = [paper["arxiv_id"] for paper in selected
                  if states.get(paper["arxiv_id"], {}).get("status") != "done"]
    return len(selected), unfinished


def run(args) -> int:
    here = Path(__file__).resolve().parent
    data = args.data_dir.resolve()
    logs = (args.log_dir or data / "logs").resolve()
    logs.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in logs.glob("performance_summary_*.json")}
    command = [sys.executable, str(here / "pipeline_m2.py"), "--data-dir", str(data),
               "--workers", str(args.workers)]
    if args.force:
        command.append("--force")
    if args.limit:
        command.extend(["--limit", str(args.limit)])
    if args.no_export:
        command.append("--no-export")
    wrapper = [sys.executable, str(here / "benchmark.py"), "--data-dir", str(data),
               "--output-dir", str(logs), "--interval", str(args.interval),
               "--disk-interval", str(args.disk_interval), "--"] + command
    exit_code = subprocess.call(wrapper)
    summaries = [p for p in logs.glob("performance_summary_*.json") if p.name not in before]
    if len(summaries) != 1:
        print("Unable to identify one new benchmark summary; check the process/logs", file=sys.stderr)
        return exit_code or 3
    summary_path = summaries[0]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    total = None if args.no_export else total_parquet_bytes(data / "tables")
    # One failed paper does not necessarily give pipeline_m2.py a nonzero exit.
    # Verify completion from the same parse manifest that Member 1 writes.
    requested = 0
    unfinished = []
    try:
        requested, unfinished = inspect_completion(data, args.limit)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        unfinished = [f"completion_check_failed:{type(exc).__name__}:{exc}"]
    complete = exit_code == 0 and requested > 0 and not unfinished and (args.no_export or total is not None)
    final_code = 0 if complete else (exit_code or 4)
    # The CPU-only status cannot be proven by psutil; record user-declared
    # environment separately from actual runtime metadata.
    cpu_only = args.cpu_only_declared
    details = {"run_id": summary.get("run_id"),
               "started_utc": summary.get("started_utc"), "ended_utc": summary.get("ended_utc"),
               "status": "finished" if complete else "incomplete_or_failed",
               "exit_code": final_code, "wall_seconds": summary.get("wall_seconds"),
               "ram_sampled_peak_bytes": summary.get("ram_sampled_peak_bytes"),
               "ram_mean_bytes": summary.get("ram_mean_bytes"),
               "parquet_final_bytes": total,
               "data_dir_sampled_peak_bytes": summary.get("data_dir_sampled_peak_bytes"),
               "workers": args.workers, "paper_limit": args.limit or "all",
               "force_reparse": int(args.force),
               "cpu_only_claim": "declared" if cpu_only else "unverified",
               "environment": args.environment,
               "benchmark_summary": str(summary_path),
               "notes": "Sampled RSS includes child-process RSS (shared pages may be double counted); "
                        "RAM and disk peaks are sampled, not strict instantaneous peaks; "
                        "Parquet bytes are final output, not pre-dedup storage; "
                        "force=0 may reuse checkpoint data; "
                        f"selected_papers={requested}, incomplete_papers={len(unfinished)}, "
                        f"first_incomplete={unfinished[:10]}, pipeline_exit_code={exit_code}; "
                        f"no_export={int(args.no_export)}"}
    out = logs / "performance_log_m2.csv"
    with out.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if f.tell() == 0:
            writer.writeheader()
        writer.writerow(details)
    print(f"M2 benchmark: exit={final_code}; wall={details['wall_seconds']}s; "
          f"peak RSS={details['ram_sampled_peak_bytes']}B; final Parquet={total}B")
    print(f"Summary: {summary_path}\nLog: {out}")
    if unfinished:
        print(f"INCOMPLETE: {len(unfinished)}/{requested} papers not done; first IDs: {unfinished[:10]}",
              file=sys.stderr)
    return final_code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--log-dir", type=Path)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-export", action="store_true")
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--disk-interval", type=float, default=30)
    p.add_argument("--environment", default="local", help="local / google_colab")
    p.add_argument("--cpu-only-declared", action="store_true",
                   help="Use only after checking Runtime -> CPU in Colab; not an automatic check")
    args = p.parse_args(argv)
    if args.workers < 1 or args.limit < 0 or args.interval <= 0 or args.disk_interval <= 0:
        p.error("workers>=1, limit>=0, sample intervals>0 required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
