"""Crawl one or more ranges in steps that survive a dying Colab runtime.

The group's benchmark crawls every member's range (5 x 5000 IDs) on Colab:
6 to 12 hours, more than one runtime can be counted on, and Google Drive is
too slow to hold the data while the crawler works on it (see snapshot.py).
This driver keeps each range's data folder on the runtime's own disk, runs
the crawler in steps under benchmark.py, and saves an incremental archive of
the folder to Drive after every step. On a new runtime the same command
restores the folders and goes on with the first step not done yet, so a dead
runtime costs at most the step that was running.

For each range of the plan, one after the other (one crawler at a time: the
arXiv rate limits "apply to all of the machines under your control as a
whole", arXiv API Terms of Use):
  1. discovery of the whole range (main.py --stages discovery);
  2. metadata, download and references, `chunk_size` papers at a time
     (main.py --start A --end B --stages metadata,download,references
     --retry-passes 0, plus --no-arxiv-dates with kaggle_dates);
  3. with kaggle_dates: ref_dates_kaggle.py --download, which takes the
     dates of the cited papers from arXiv's metadata on Kaggle instead of
     some 700 arXiv API calls per 5000 papers (the date caches of the ranges
     done before are passed with --cache); if it fails, step 4 asks arXiv;
  4. a sweep of the whole range with every stage and main.py's retry passes,
     for what the chunks left (a file put aside, a failed arXiv batch...) and
     the dates still missing. Chunks leave their failures to the sweep on
     purpose: a file arXiv could not serve gets the time of the following
     chunks to come back, and no chunk waits for retry passes.
When every range is done, each range with something still left gets one
more sweep ("late sweep N", one per run of this command).

Every step runs under benchmark.py with its measurements written straight
into <backup_dir>/perf, so a step whose runtime died still leaves its last
heartbeat (group_benchmark.py lists such runs). They are copied into
<data_dir>/logs, where benchmark_runs.py and group_benchmark.py read them.
The steps done are recorded in <data_dir>/work/chunk_journal.jsonl, which the
archives carry, so a restored folder knows which steps its files hold.
Saving and restoring archives is outside the measured crawl time; it is
logged in <backup_dir>/snapshot_log.csv.

    python src/chunked_crawl.py /content/g11/plan.json
    python src/chunked_crawl.py /content/g11/plan.json --restore-only

plan.json (written by the Colab notebook):
    {"chunk_size": 1000, "kaggle_dates": true, "max_wait_minutes": 30,
     "runs": [{"name": "AQuan", "start": "2605.12338", "end": "2605.17337",
               "data_dir": "/content/g11/AQuan_2605-12338_2605-17337",
               "backup_dir": ".../group11-data/03_benchmark/AQuan_2605-12338_2605-17337"}]}
"kaggle_snapshot": a copy of the Kaggle file already on disk, used instead of
downloading it (ref_dates_kaggle.py --snapshot).

Exit codes: 0 every range complete; 4 something still left (run the same
command again later); 3 arXiv kept refusing (wait 1 to 2 hours); 130 stopped
by the user; 1 a step kept failing, or saving to Drive failed (read above).
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import snapshot
from common import SUCCESS, CsvLog, DataLayout, Manifest, now_iso
from discovery import RangeBound, known_records
from main import LEFT_KEYS

SRC = Path(__file__).resolve().parent
JOURNAL = "work/chunk_journal.jsonl"
SNAPSHOT_LOG_FIELDS = ["time", "run", "step", "action", "archive", "seconds", "bytes"]
EXIT_LEFT = 4
# Waits before a step is tried again, by main.py exit code, and how many tries in all.
REFUSED_WAIT, LOCK_WAIT, ERROR_WAIT = 15 * 60, 200, 60  # exit 3 / exit 2 / other errors
TRIES = {3: 2, 2: 3}  # other non-zero exit codes: 2 tries


class Stop(Exception):
    """A step failed for good: the driver stops with `code`."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Run:
    name: str
    start: str
    end: str
    data_dir: Path
    backup_dir: Path | None

    @property
    def archives(self) -> Path | None:
        return self.backup_dir / "snapshots" if self.backup_dir else None

    @property
    def perf(self) -> Path:
        return self.backup_dir / "perf" if self.backup_dir else self.data_dir / "logs"


def read_journal(run: Run) -> list[dict]:
    path = run.data_dir / JOURNAL
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # line cut by a crash
    return records


def done_steps(run: Run) -> set[str]:
    """Steps whose latest try ended with exit code 0."""
    last = {r["step"]: r.get("rc") for r in read_journal(run) if "step" in r}
    return {step for step, rc in last.items() if rc == 0}


def journal(run: Run, record: dict) -> None:
    path = run.data_dir / JOURNAL
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_snapshot(run: Run, step: str, action: str, archive: str, seconds: float, size: int) -> None:
    if run.backup_dir:
        CsvLog(run.backup_dir / "snapshot_log.csv", SNAPSHOT_LOG_FIELDS).write(
            time=now_iso(), run=run.name, step=step, action=action, archive=archive,
            seconds=round(seconds, 1), bytes=size)


def save(run: Run, step: str) -> None:
    """Archive what changed in the data folder (see snapshot.py)."""
    if not run.backup_dir:
        return
    started = time.perf_counter()
    try:
        path = snapshot.save(run.data_dir, run.archives, step)
    except OSError as exc:
        raise Stop(1, f"{run.name}: could not save to {run.archives} ({exc}). The data of this "
                      f"runtime is still on its disk: free space on Drive, then run again") from exc
    if path:
        seconds, size = time.perf_counter() - started, path.stat().st_size
        print(f"[{run.name}] saved {path.name} ({size / 2**20:.1f} MB) in {seconds:.0f} s", flush=True)
        log_snapshot(run, step, "save", path.name, seconds, size)


def copy_perf(run: Run) -> None:
    """Copy benchmark.py's files from <backup_dir>/perf into <data_dir>/logs."""
    if not run.backup_dir or not run.perf.is_dir():
        return
    logs = DataLayout(run.data_dir).logs
    logs.mkdir(parents=True, exist_ok=True)
    for path in run.perf.glob("performance_*"):
        target = logs / path.name
        if not target.exists() or target.stat().st_size != path.stat().st_size:
            shutil.copy2(path, target)


def restore(run: Run) -> None:
    if not run.backup_dir:
        return
    started = time.perf_counter()
    count = snapshot.restore(run.data_dir, run.archives)
    if count:
        seconds = time.perf_counter() - started
        print(f"[{run.name}] restored {count} archives from {run.archives} in {seconds:.0f} s",
              flush=True)
        log_snapshot(run, "", "restore", f"{count} archives", seconds, 0)
    copy_perf(run)


def launch(run: Run, script: str, args: list[str]) -> int:
    """Run src/<script> under benchmark.py; returns its exit code."""
    run.perf.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(SRC / "benchmark.py"), "--data-dir", str(run.data_dir),
               "--output-dir", str(run.perf), "--interval", "2", "--disk-interval", "300",
               "--heartbeat-seconds", "60", "--", sys.executable, str(SRC / script), *args]
    process = subprocess.Popen(command)
    try:
        return process.wait()
    except KeyboardInterrupt:
        try:
            process.wait(timeout=120)  # Stop reached the step too: let it write its summary
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            process.kill()
        raise


def run_step(run: Run, step: str, script: str, args: list[str], *, optional: bool = False) -> None:
    """Run one step until it succeeds, saving the data folder after each try.
    Raises Stop when it keeps failing (an optional step is only reported)."""
    for attempt in range(1, 10):
        print(f"\n[{run.name}] {step}", flush=True)
        started, clock = now_iso(), time.perf_counter()
        code = launch(run, script, args)
        journal(run, {"step": step, "rc": code, "started": started, "ended": now_iso(),
                      "seconds": round(time.perf_counter() - clock, 1)})
        copy_perf(run)
        save(run, step)
        if code == 0:
            return
        if code == 130:
            raise KeyboardInterrupt
        if optional:
            print(f"[{run.name}] {step}: exit code {code}, going on without it", flush=True)
            return
        if attempt >= TRIES.get(code, 2):
            raise Stop(3 if code == 3 else 1,
                       f"{run.name}: {step} ended with exit code {code} {attempt} times")
        wait = REFUSED_WAIT if code == 3 else LOCK_WAIT if code == 2 else ERROR_WAIT
        print(f"[{run.name}] {step}: exit code {code}, trying again in {wait / 60:.0f} min",
              flush=True)
        time.sleep(wait)


def chunks_of(run: Run, size: int) -> list[list[str]]:
    """[first ID, last ID] of every `size` papers found by discovery, in
    order; computed once and kept in the journal, so they never move."""
    for record in read_journal(run):
        if record.get("step") == "plan":
            return record["chunks"]
    records = known_records(Manifest(DataLayout(run.data_dir).manifest),
                            RangeBound.parse(run.start), RangeBound.parse(run.end))
    ids = [r["arxiv_id"] for r in records if r["status"] == SUCCESS]
    chunks = [[ids[i], ids[min(i + size, len(ids)) - 1]] for i in range(0, len(ids), size)]
    journal(run, {"step": "plan", "chunks": chunks, "papers": len(ids)})
    return chunks


def left(run: Run) -> dict[str, int]:
    """What the latest pass of main.py left (logs/pass_log.csv)."""
    path = DataLayout(run.data_dir).logs / "pass_log.csv"
    rows = []
    if path.is_file():
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    last = rows[-1] if rows else {}
    return {key: int(float(last.get(key) or 0)) for key in LEFT_KEYS}


def crawl_range(run: Run, plan: dict, earlier: list[Run]) -> None:
    restore(run)
    done = done_steps(run)
    data = ["--data-dir", str(run.data_dir), "--max-wait-minutes", str(plan["max_wait_minutes"])]
    whole = ["--start", run.start, "--end", run.end] + data
    if "discovery" not in done:
        run_step(run, "discovery", "main.py", whole + ["--stages", "discovery", "--retry-passes", "1"])
    chunks = chunks_of(run, plan["chunk_size"])
    for number, (first, last) in enumerate(chunks, 1):
        step = f"chunk {first}-{last}"
        if step not in done:
            print(f"[{run.name}] chunk {number}/{len(chunks)}", flush=True)
            run_step(run, step, "main.py",
                     ["--start", first, "--end", last] + data
                     + ["--stages", "metadata,download,references", "--retry-passes", "0"]
                     + (["--no-arxiv-dates"] if plan["kaggle_dates"] else []))
    if plan["kaggle_dates"] and "kaggle_dates" not in done:
        caches = [str(r.data_dir / "work" / "ref_arxiv_dates.jsonl") for r in earlier
                  if (r.data_dir / "work" / "ref_arxiv_dates.jsonl").is_file()]
        source = (["--snapshot", plan["kaggle_snapshot"]] if plan.get("kaggle_snapshot")
                  else ["--download"])
        run_step(run, "kaggle_dates", "ref_dates_kaggle.py",
                 ["--data-dir", str(run.data_dir)] + source + (["--cache", *caches] if caches else []),
                 optional=True)
    if "sweep" not in done:
        run_step(run, "sweep", "main.py", whole)


def late_sweep(run: Run, plan: dict) -> None:
    """One more sweep of a range with something left, numbered per run of this command."""
    done = done_steps(run)
    number = 1 + sum(1 for step in done if step.startswith("late sweep "))
    run_step(run, f"late sweep {number}", "main.py",
             ["--start", run.start, "--end", run.end, "--data-dir", str(run.data_dir),
              "--max-wait-minutes", str(plan["max_wait_minutes"])])


def status(run: Run) -> str:
    records = Manifest(DataLayout(run.data_dir).manifest).load()
    papers = sum(1 for r in records.values() if r.get("status") == SUCCESS)
    todo = {k: v for k, v in left(run).items() if v}
    steps = done_steps(run)
    chunks = next((r["chunks"] for r in read_journal(run) if r.get("step") == "plan"), None)
    done_chunks = sum(1 for s in steps if s.startswith("chunk "))
    where = ("not started" if not steps else
             f"discovery done, chunks {done_chunks}/{len(chunks) if chunks is not None else '?'}"
             + (", sweep done" if "sweep" in steps else ""))
    return (f"{run.name}: {papers} papers | {where} | "
            + (", ".join(f"{k}={v}" for k, v in todo.items()) if todo else "nothing left"))


def load_plan(path: Path) -> tuple[dict, list[Run]]:
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    plan.setdefault("chunk_size", 1000)
    plan.setdefault("kaggle_dates", False)
    plan.setdefault("max_wait_minutes", 30)
    runs = [Run(r["name"], r["start"], r["end"], Path(r["data_dir"]),
                Path(r["backup_dir"]) if r.get("backup_dir") else None) for r in plan["runs"]]
    return plan, runs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Crawl ranges in steps, saving each step to Drive")
    parser.add_argument("plan", type=Path, help="plan.json (see the module docstring)")
    parser.add_argument("--restore-only", action="store_true",
                        help="only restore the data folders from their backups and show where they are")
    args = parser.parse_args(argv)
    plan, runs = load_plan(args.plan)
    try:
        for run in runs:
            restore(run)
        if not args.restore_only:
            for number, run in enumerate(runs):
                crawl_range(run, plan, runs[:number])
            for run in runs:
                if any(left(run).values()):
                    late_sweep(run, plan)
    except KeyboardInterrupt:
        print("\nStopped by the user. Every finished step is saved; run the same command "
              "again to continue.", flush=True)
        return 130
    except Stop as exc:
        print(f"\nStopped: {exc}", flush=True)
        return exc.code
    finally:
        print("\n== Ranges ==")
        for run in runs:
            print("  " + status(run))
    if args.restore_only:
        return 0
    return EXIT_LEFT if any(any(left(run).values()) for run in runs) else 0


if __name__ == "__main__":
    sys.exit(main())
