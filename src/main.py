"""Milestone 1 pipeline: discovery -> metadata -> (source download || references).

Usage, from the repository root (locally or in a Colab cell with "!"). Every
member runs the same code with their own range from the registration sheet:

    python src/main.py --range 2026-05 12338 2026-05 17337 --data-dir data
    python src/main.py --range 2025-12 25000 2026-01 100 --data-dir data --limit 10
    python src/main.py --start 2605.12338 --end 2605.17337 --data-dir data --stages discovery,metadata

--range takes the four sheet columns as they are (start_month start_id
end_month end_id); --start/--end is the same range written as two IDs. The
range may span several months or years. --limit N is a trial run on the
first N IDs only.

Outputs under --data-dir (keep it outside the code repository):
    work/paper_manifest.jsonl      one record per ID: status and version list
    corpus/<yymm-id>/metadata.json paper metadata (metadata.py)
    corpus/<yymm-id>/v1/, v2/ ...  .tex/.bib sources (downloader.py, Member 2)
    corpus/<yymm-id>/references.json  references (references.py, Member 3)
    logs/arxiv_api_log.csv         one row per arXiv API call
    logs/run_log.csv               start, end and duration of every stage
    logs/arxiv_throttle_log.csv    pauses after arXiv refused or did not answer (rate_limiter.py)
    work/crawler.lock              present while a crawler runs on this folder

The download and references stages run at the same time: they talk to
different servers (arXiv and Semantic Scholar), each with its own rate limit.
Re-running the same command is safe: every stage skips finished work.
Pressing Stop (Ctrl+C) ends the run at once; the interrupted work is redone
by the next run. If arXiv refuses every request for --max-wait-minutes (30 by
default), the run stops with exit code 3 and says what to do (see
rate_limiter.ArxivUnavailable). Only one crawler may use a data folder at a time: a second
one refuses to start while work/crawler.lock is fresh (see common.RunLock).

Contract with the other modules. downloader.py and references.py each expose

    run(papers: list[dict], layout: common.DataLayout) -> None

where `papers` are the manifest records with status "success" (fields:
arxiv_id, folder, latest_version, versions). Every arXiv request made by
downloader.py must go through rate_limiter.ARXIV.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import threading
import time
from collections import Counter

from common import SUCCESS, CsvLog, DataLayout, Manifest, RunLock, now_iso
from discovery import API_LOG_FIELDS, RangeBound, discover_range, known_records
from metadata import collect_metadata
from rate_limiter import (ARXIV, DEFAULT_USER_AGENT, THROTTLE_LOG_FIELDS, ArxivUnavailable,
                          make_session)

STAGES = ("discovery", "metadata", "download", "references")
PARALLEL_STAGES = {"download": "downloader", "references": "references"}  # stage -> module
RUN_LOG_FIELDS = ["run_id", "stage", "started_at", "finished_at", "seconds", "items", "note"]


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="arXiv crawler - Milestone 1")
    parser.add_argument("--range", nargs=4, dest="sheet_range",
                        metavar=("START_MONTH", "START_ID", "END_MONTH", "END_ID"),
                        help="the four registration sheet columns, e.g. 2026-05 12338 2026-05 17337")
    parser.add_argument("--start", help="first ID of the range, e.g. 2605.12338 or 2026-05:12338")
    parser.add_argument("--end", help="last ID of the range, e.g. 2605.17337 or 2026-05:17337")
    parser.add_argument("--data-dir", default="data",
                        help="root folder for corpus, work files and logs (default: data)")
    parser.add_argument("--stages", default=",".join(STAGES),
                        help=f"comma-separated subset of {','.join(STAGES)} (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="trial run: only the first N IDs of the range")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="IDs per arXiv API call (default: 100)")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT,
                        help="User-Agent header sent to arXiv")
    parser.add_argument("--force", action="store_true",
                        help="start even if work/crawler.lock says another crawler uses this "
                             "data folder (only when you are sure it has stopped)")
    parser.add_argument("--max-wait-minutes", type=float, default=30.0,
                        help="stop when arXiv has refused every request for this long "
                             "(default: 30; e.g. 120 for an unattended night run; 0: never stop)")
    args = parser.parse_args(argv)
    if args.max_wait_minutes < 0:
        parser.error("--max-wait-minutes must be 0 or more")

    if args.sheet_range and (args.start or args.end):
        parser.error("use either --range or --start/--end, not both")
    try:
        if args.sheet_range:
            start_month, start_id, end_month, end_id = args.sheet_range
            args.start = RangeBound.parse(f"{start_month}:{start_id}")
            args.end = RangeBound.parse(f"{end_month}:{end_id}")
        elif args.start and args.end:
            args.start, args.end = RangeBound.parse(args.start), RangeBound.parse(args.end)
        else:
            parser.error("give the range with --range or with both --start and --end")
    except ValueError as exc:
        parser.error(str(exc))
    if (int(args.end.yymm), args.end.number) < (int(args.start.yymm), args.start.number):
        parser.error("the end of the range comes before its start")

    args.stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = set(args.stages) - set(STAGES)
    if unknown:
        parser.error(f"unknown stage(s): {', '.join(sorted(unknown))}")
    return args


class StageTimer:
    """Writes one row of logs/run_log.csv per stage (wall time, item count)."""

    def __init__(self, log: CsvLog, run_id: str, stage: str):
        self.log, self.run_id, self.stage = log, run_id, stage
        self.items, self.note = "", ""

    def __enter__(self):
        self.started_at = now_iso()
        self._start = time.perf_counter()
        print(f"[{self.stage}] started")
        return self

    def __exit__(self, exc_type, exc, tb):
        seconds = round(time.perf_counter() - self._start, 3)
        note = self.note or (f"{exc_type.__name__}: {exc}" if exc_type else "")
        self.log.write(run_id=self.run_id, stage=self.stage, started_at=self.started_at,
                       finished_at=now_iso(), seconds=seconds, items=self.items, note=note)
        print(f"[{self.stage}] finished in {seconds:.1f} s {note}".rstrip())
        return False


def load_stage(stage: str):
    """Import the run() function of a teammate's module, or None if the module
    has not been added to src/ yet."""
    module_name = PARALLEL_STAGES[stage]
    try:
        return importlib.import_module(module_name).run
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise  # the module exists but one of its own imports is missing
        print(f"[{stage}] skipped: src/{module_name}.py is not available yet")
        return None


def run_timed(stage: str, func, papers: list[dict], layout: DataLayout,
              run_log: CsvLog, run_id: str) -> None:
    with StageTimer(run_log, run_id, stage) as timer:
        timer.items = len(papers)
        func(papers, layout)


def run_parallel(runners: dict, papers: list[dict], layout: DataLayout,
                 run_log: CsvLog, run_id: str) -> None:
    """Run the download and references stages at the same time.

    Daemon threads with short joins rather than a ThreadPoolExecutor: when the
    user presses Stop (Ctrl+C) the main thread gets control back at once and
    the program can end, instead of the executor waiting for every remaining
    download. Work cut off this way is redone by the next run.
    """
    errors: list[BaseException] = []

    def stage_thread(stage, func):
        try:
            run_timed(stage, func, papers, layout, run_log, run_id)
        except BaseException as exc:  # handed over to the main thread below
            errors.append(exc)

    threads = [threading.Thread(target=stage_thread, args=item, name=item[0], daemon=True)
               for item in runners.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        while thread.is_alive():
            thread.join(timeout=1.0)  # short waits keep Ctrl+C responsive
    if errors:
        raise errors[0]


def main(argv=None) -> int:
    args = parse_args(argv)
    layout = DataLayout(args.data_dir)
    layout.create()
    lock = RunLock(layout.work / "crawler.lock")
    try:
        lock.acquire(force=args.force)
    except RuntimeError as exc:
        print(f"Not started: {exc}")
        return 2
    try:
        return crawl(args, layout)
    except KeyboardInterrupt:
        print("\nStopped by the user. Finished papers and versions are saved; "
              "run the same command again to continue.", flush=True)
        return 130
    except ArxivUnavailable as exc:
        print(f"\nStopped: {exc}", flush=True)
        return 3
    finally:
        lock.release()


def crawl(args: argparse.Namespace, layout: DataLayout) -> int:
    """Run the selected stages for the range of `args` (see the module docstring)."""
    start, end = args.start, args.end
    manifest = Manifest(layout.manifest)
    api_log = CsvLog(layout.logs / "arxiv_api_log.csv", API_LOG_FIELDS)
    run_log = CsvLog(layout.logs / "run_log.csv", RUN_LOG_FIELDS)
    ARXIV.log = CsvLog(layout.logs / "arxiv_throttle_log.csv", THROTTLE_LOG_FIELDS)
    ARXIV.give_up_after = args.max_wait_minutes * 60
    session = make_session(args.user_agent)
    run_id = now_iso()
    print(f"Range {start.as_id()} .. {end.as_id()} | data: {layout.root} | stages: {', '.join(args.stages)}")

    # 1. Discovery: which IDs exist and how many versions each has.
    if "discovery" in args.stages:
        with StageTimer(run_log, run_id, "discovery") as timer:
            records = discover_range(start, end, session, manifest, api_log,
                                     batch_size=args.batch_size, limit=args.limit)
            timer.items = len(records)
        counts = Counter(r["status"] for r in records)
        print(f"  {len(records)} IDs: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        records = known_records(manifest, start, end)
        if args.limit is not None:
            records = records[:args.limit]

    papers = [r for r in records if r["status"] == SUCCESS]
    if not papers:
        print("No paper found in this range yet; run the discovery stage first.")
        return 0

    # 2. Metadata: corpus/<id>/metadata.json for every paper.
    if "metadata" in args.stages:
        with StageTimer(run_log, run_id, "metadata") as timer:
            written, incomplete = collect_metadata(papers, layout, session, api_log,
                                                   batch_size=args.batch_size)
            timer.items = written
            if incomplete:
                timer.note = f"{len(incomplete)} papers incomplete, re-run to retry"
        print(f"  metadata.json written for {written} papers")

    # 3. Source download (Member 2) and references (Member 3), in parallel.
    runners = {stage: load_stage(stage) for stage in PARALLEL_STAGES if stage in args.stages}
    runners = {stage: func for stage, func in runners.items() if func is not None}
    if runners:
        run_parallel(runners, papers, layout, run_log, run_id)

    if ARXIV.throttle_count:
        print(f"arXiv refused or did not answer {ARXIV.throttle_count} times; requests were "
              f"paused {ARXIV.paused_seconds:.0f} s in total (logs/arxiv_throttle_log.csv)")
    return 0


if __name__ == "__main__":
    code = main()
    if code == 130:
        # Stopped by the user: end now instead of waiting for the stage threads.
        sys.stdout.flush()
        os._exit(code)
    sys.exit(code)
