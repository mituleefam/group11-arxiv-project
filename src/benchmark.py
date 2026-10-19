"""Role 4: launch a crawler command and monitor elapsed time, RSS, and output storage.

CPU-only final benchmark must be executed on Google Colab (per assignment).

Example (from repository root):
  python src/benchmark.py --data-dir /path/to/member4 --interval 2 \
      --disk-interval 30 -- python src/main.py --start 2511.21709 \
      --end 2512.03230 --data-dir /path/to/member4 --limit 10

The command after `--` runs exactly once. No Semantic Scholar key required.
Peak disk size here means *sampled data-dir size*, not necessarily the exact
peak of all temporary files on the Colab machine; note measurement caveats.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
except ImportError as exc:
    raise SystemExit("Missing psutil. Run: python -m pip install psutil") from exc


def directory_bytes(root: Path) -> int:
    """Non-following directory scan; measures files under the data root."""
    if not root.exists():
        return 0
    total = 0
    for directory, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            file_path = Path(directory) / name
            try:
                if not file_path.is_symlink():
                    total += file_path.stat().st_size
            except (FileNotFoundError, PermissionError):
                pass
    return total


def rss_tree(pid: int) -> tuple[int, int]:
    """RSS sum of launched process and live descendants (process shared RSS may overlap)."""
    try:
        root = psutil.Process(pid)
        processes = [root] + root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0, 0
    total, n = 0, 0
    for proc in processes:
        try:
            with proc.oneshot():
                total += proc.memory_info().rss
            n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return total, n


def iso_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(command: list[str], data_dir: Path, output_dir: Path, interval: float, disk_interval: float):
    if not command:
        raise ValueError("Crawler command required after --")
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    samples_file = output_dir / f"performance_samples_{run_id}.csv"
    summary_file = output_dir / f"performance_summary_{run_id}.json"
    fields = ["timestamp_utc", "elapsed_seconds", "rss_tree_bytes", "process_count", "data_dir_bytes"]
    sample_ram = []
    sample_disk = []
    disk_value = directory_bytes(data_dir)
    last_disk_at = time.monotonic()
    start_iso = iso_utc()
    start = time.perf_counter()
    proc = None
    rc = None
    interrupted = False
    print("Starting crawler:", " ".join(command), flush=True)
    print("Data directory:", data_dir, flush=True)
    print("Samples:", samples_file, flush=True)
    with samples_file.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        try:
            proc = subprocess.Popen(command)
            while True:
                now = time.monotonic()
                if now - last_disk_at >= disk_interval:
                    disk_value = directory_bytes(data_dir)
                    last_disk_at = now
                rss, nprocs = rss_tree(proc.pid)
                elapsed = time.perf_counter() - start
                row = dict(timestamp_utc=iso_utc(), elapsed_seconds=round(elapsed, 3),
                           rss_tree_bytes=rss, process_count=nprocs, data_dir_bytes=disk_value)
                writer.writerow(row)
                out.flush()
                if nprocs:
                    sample_ram.append(rss)
                sample_disk.append(disk_value)
                rc = proc.poll()
                if rc is not None:
                    break
                try:
                    proc.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    pass
        except KeyboardInterrupt:
            interrupted = True
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            rc = 130
        finally:
            final_data_bytes = directory_bytes(data_dir)
            sample_disk.append(final_data_bytes)
            elapsed = time.perf_counter() - start
            summary = {
                "run_id": run_id, "command": command, "started_utc": start_iso,
                "ended_utc": iso_utc(), "return_code": rc, "interrupted": interrupted,
                "wall_seconds": round(elapsed, 3), "sample_interval_seconds": interval,
                "disk_scan_interval_seconds": disk_interval,
                "ram_mean_bytes": sum(sample_ram) / len(sample_ram) if sample_ram else None,
                "ram_sampled_peak_bytes": max(sample_ram) if sample_ram else None,
                "data_dir_sampled_peak_bytes": max(sample_disk) if sample_disk else final_data_bytes,
                "data_dir_final_bytes": final_data_bytes,
                "data_dir": str(data_dir),
                "measurement_notes": [
                    "RAM is the sum of RSS of the child process and its descendants (shared memory may be counted more than once).",
                    "Peak RAM and data-dir size are sampled: peaks between samples may be missed.",
                    "Data-dir scan does not include other temporary filesystem paths outside data-dir.",
                    "If data-dir is on Google Drive, scan latency and remote I/O affect measured runtime.",
                    "A rerun of a checkpointed corpus measures resume/skip, not a fresh crawl.",
                ],
            }
            with summary_file.open("w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
            print("\nBenchmark completed; return code:", rc, flush=True)
            print("Wall seconds:", round(elapsed, 3), flush=True)
            print("Sampled peak RAM (MiB):", round(max(sample_ram) / (1024 ** 2), 2) if sample_ram else None)
            print("Sampled peak data-dir (MiB):", round(summary["data_dir_sampled_peak_bytes"] / (1024 ** 2), 2))
            print("Summary:", summary_file, flush=True)
    return 130 if interrupted else int(rc or 0)


def main(argv=None):
    p = argparse.ArgumentParser(description="Run and monitor Group 11 crawler on Colab CPU")
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--interval", type=float, default=2, help="RAM sample interval in seconds")
    p.add_argument("--disk-interval", type=float, default=30, help="Corpus-size sample interval; Drive scans can be slow")
    p.add_argument("command", nargs=argparse.REMAINDER, help="Use '-- python src/main.py ...'")
    args = p.parse_args(argv)
    cmd = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not cmd:
        p.error("Provide crawler command after --")
    if args.interval <= 0 or args.disk_interval <= 0:
        p.error("Intervals must be positive")
    if cmd[0] == "python":
        cmd[0] = sys.executable
    return run(cmd, args.data_dir, args.output_dir or (args.data_dir / "logs"), args.interval, args.disk_interval)


if __name__ == "__main__":
    raise SystemExit(main())
