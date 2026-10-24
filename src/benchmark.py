"""Role 4: end-to-end wall time, sampled process RAM and disk footprint.

Example: python src/benchmark.py --data-dir data --interval 2 \
  --disk-interval 300 -- python src/main.py --start 2511.21709 \
  --end 2512.03230 --data-dir data

Use on Colab CPU-only for the required final benchmark. Remote Google Drive
scans are executed in a background thread so they do not block RAM sampling.
The reported data-dir peak is a *sampled* size, not guaranteed precise peak.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
except ImportError as exc:
    raise SystemExit("Install psutil with python -m pip install psutil") from exc


def directory_bytes(root: Path) -> int:
    """Estimate data tree size without following symlinks."""
    if not root.exists():
        return 0
    total = 0
    for directory, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except (FileNotFoundError, PermissionError, OSError):
                continue
    return total


def rss_tree(pid: int) -> tuple[int, int]:
    """Sample sum of RSS across process and child processes; shared pages may overlap."""
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0, 0
    used = 0
    counted = 0
    for proc in procs:
        try:
            used += proc.memory_info().rss
            counted += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return used, counted


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(command: list[str], data_dir: Path, output_dir: Path,
        interval: float = 2, disk_interval: float = 300,
        heartbeat_seconds: float = 60) -> int:
    if not command:
        raise ValueError("Crawler command required")
    if interval <= 0 or disk_interval <= 0 or heartbeat_seconds <= 0:
        raise ValueError("Sample intervals must be positive")
    # Capture freshness BEFORE creating benchmark log files. A resumed run
    # cannot be described as a fresh end-to-end crawl of the full corpus.
    existing_manifest = (data_dir / "work" / "paper_manifest.jsonl").exists()
    corpus_root = data_dir / "corpus"
    existing_corpus = False
    if corpus_root.is_dir():
        try:
            existing_corpus = next(corpus_root.iterdir(), None) is not None
        except OSError:
            existing_corpus = True  # Unknown provenance must not be marked fresh.
    started_fresh = not existing_manifest and not existing_corpus
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    samples_path = output_dir / f"performance_samples_{run_id}.csv"
    summary_path = output_dir / f"performance_summary_{run_id}.json"
    progress_path = output_dir / f"performance_progress_{run_id}.json"
    columns = ["timestamp_utc", "elapsed_seconds", "rss_tree_bytes", "process_count",
               "data_dir_bytes", "colab_disk_used_bytes"]
    samples_ram = []
    samples_corpus = []
    samples_local_disk = []
    local_base = None
    local_path = Path("/content") if Path("/content").exists() else Path.cwd()
    latest = {"size": None, "error": ""}
    lock = threading.Lock()
    stop = threading.Event()
    disk_thread = None
    started_utc = utc_now()
    started = time.perf_counter()
    proc = None
    rc = None
    interrupted = False
    elapsed_wall = None
    last_heartbeat = -float("inf")

    def write_heartbeat(elapsed: float, *, final: bool = False):
        # Best-effort write through atomic rename. Survives Colab runtime crashes
        # more often than end-only summaries; never pretend it is a final result.
        state = {
            "run_id": run_id, "started_utc": started_utc,
            "last_observed_utc": utc_now(), "observed_elapsed_seconds": round(elapsed, 3),
            "status": "finished" if final else "running_or_interrupted_unverified",
            "return_code": rc if final else None,
            "ram_sampled_peak_so_far_bytes": max(samples_ram) if samples_ram else None,
            "data_dir_sampled_peak_so_far_bytes": max(samples_corpus) if samples_corpus else None,
            "command": command,
            "NOTE": "Only summary JSON proves this invocation ended; a heartbeat is partial evidence."
        }
        tmp = progress_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, progress_path)

    def disk_scanner():
        while not stop.is_set():
            try:
                size = directory_bytes(data_dir)
                with lock:
                    latest["size"] = size
            except OSError as exc:
                with lock:
                    latest["error"] = repr(exc)
            if stop.wait(disk_interval):
                break

    print("Starting:", " ".join(command), flush=True)
    print("Writing samples to:", samples_path, flush=True)
    with samples_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        try:
            local_base = shutil.disk_usage(local_path).used
            proc = subprocess.Popen(command)
            disk_thread = threading.Thread(target=disk_scanner, daemon=True)
            disk_thread.start()
            while True:
                rss, nprocs = rss_tree(proc.pid)
                with lock:
                    corpus_size = latest["size"]
                local_used = shutil.disk_usage(local_path).used
                row = {
                    "timestamp_utc": utc_now(),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "rss_tree_bytes": rss,
                    "process_count": nprocs,
                    "data_dir_bytes": "" if corpus_size is None else corpus_size,
                    "colab_disk_used_bytes": local_used,
                }
                writer.writerow(row)
                handle.flush()  # Preserve samples even if Colab disconnects
                if row["elapsed_seconds"] - last_heartbeat >= heartbeat_seconds:
                    write_heartbeat(row["elapsed_seconds"])
                    last_heartbeat = row["elapsed_seconds"]
                if nprocs:
                    samples_ram.append(rss)
                if corpus_size is not None:
                    samples_corpus.append(corpus_size)
                samples_local_disk.append(local_used)
                rc = proc.poll()
                if rc is not None:
                    elapsed_wall = time.perf_counter() - started
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
            elapsed_wall = time.perf_counter() - started
        finally:
            stop.set()
            if elapsed_wall is None:
                elapsed_wall = time.perf_counter() - started
            # Snapshot AFTER stopping the scraper; cost is NOT added to wall time.
            print("Crawler stopped; measuring final data size...", flush=True)
            final_size = directory_bytes(data_dir)
            samples_corpus.append(final_size)
            disk_error = latest["error"]
            result = {
                "run_id": run_id, "command": command,
                "started_with_fresh_data_dir": started_fresh,
                "existing_manifest_at_start": existing_manifest,
                "existing_corpus_at_start": existing_corpus,
                "started_utc": started_utc, "ended_utc": utc_now(),
                "return_code": rc, "interrupted": interrupted,
                "wall_seconds": round(elapsed_wall, 3),
                "sample_interval_seconds": interval, "disk_scan_interval_seconds": disk_interval,
                "ram_mean_bytes": sum(samples_ram)/len(samples_ram) if samples_ram else None,
                "ram_sampled_peak_bytes": max(samples_ram) if samples_ram else None,
                "data_dir_sampled_peak_bytes": max(samples_corpus) if samples_corpus else None,
                "data_dir_final_bytes": final_size,
                "colab_disk_used_baseline_bytes": local_base,
                "colab_disk_sampled_peak_used_bytes": max(samples_local_disk) if samples_local_disk else None,
                "colab_disk_sampled_max_increase_bytes":
                    max(0, max(samples_local_disk)-local_base) if samples_local_disk and local_base is not None else None,
                "data_dir": str(data_dir), "disk_scan_error": disk_error,
                "measurement_notes": [
                    "RAM sums RSS for child+descendant processes; shared pages may be double counted.",
                    "Peaks are sampled; instantaneous peaks can be missed.",
                    "Data-dir scan is asynchronous and may lag behind actual file writes.",
                    "Local Colab disk used includes all processes and baseline; delta is not necessarily crawler-only.",
                    "Google Drive mounted corpus is a remote filesystem and is not measured by local disk free-space metrics.",
                    "Runtime excludes post-run final directory scan; interruptions may still prevent summary writing.",
                    "Checkpoint resume is not a fresh end-to-end run.",
                    "Freshness is inferred from manifest/corpus presence at start; verify actual work in stage logs.",
                ]
            }
            with summary_path.open("w", encoding="utf-8") as summary_file:
                json.dump(result, summary_file, indent=2, ensure_ascii=False)
            print("Crawler return code:", rc, flush=True)
            print("Wall seconds:", round(elapsed_wall, 3), flush=True)
            write_heartbeat(elapsed_wall, final=True)
            print("Output:", summary_path, flush=True)
    return 130 if interrupted else int(rc or 0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Colab CPU-only crawler benchmark")
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--interval", type=float, default=2)
    p.add_argument("--disk-interval", type=float, default=300)
    p.add_argument("--heartbeat-seconds", type=float, default=60,
                   help="Write partial progress JSON periodically; does not replace the final summary")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    cmd = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not cmd:
        p.error("Pass command after --")
    if cmd[0] == "python":
        cmd[0] = sys.executable
    return run(cmd, args.data_dir, args.output_dir or args.data_dir / "logs",
               args.interval, args.disk_interval, args.heartbeat_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
