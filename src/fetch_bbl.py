"""Add the .bbl files of the versions that lost their references.

The first crawl kept only .tex and .bib files, so a paper whose references are
only in a .bbl has none in the corpus. extractor.py now keeps .bbl files, and
this script repairs a corpus already crawled, without crawling it again:

  1. gather every version on disk and pick those with citations and no source
     of references (gatherer.Gathered.needs_bbl);
  2. download only those versions, at the shared arXiv pace, and copy their .bbl
     files into the version folder (nothing else is written, no file replaced);
  3. add the files to kept_files and kept_bytes in versions.json, so validate.py
     stays consistent.

    python src/fetch_bbl.py --data-dir data --dry-run    # how many, no request sent
    python src/fetch_bbl.py --data-dir data
    python src/fetch_bbl.py --data-dir data --ids 2604.05535

Run it before pipeline_m2.py, which then reads the .bbl files. Running it again
goes on where it stopped: a version that got its .bbl is no longer picked, and
one whose source has no .bbl or is gone (logs/bbl_log.csv) is asked only with
--retry-no-bbl. Failed downloads are tried again. It holds work/crawler.lock, so
it cannot run together with a crawler on the same folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from checkpoint import VersionStore
from common import SUCCESS, CsvLog, DataLayout, RunLock, atomic_write_json, now_iso
from downloader import fetch_source
from extractor import CorruptSourceError, IncompleteSourceError, extract_source
from gatherer import gather
from rate_limiter import ARXIV_EXPORT, make_session

LOG_FIELDS = ["time", "arxiv_id", "version", "status", "bbl_files", "bbl_bytes", "http_status", "error"]
ADDED, NO_BBL, GONE, FAILED = "added", "no_bbl", "not_found", "failed"
DOWNLOAD_WORKERS = 6


def scan_paper(paper_dir: str) -> dict:
    """The versions of a paper that need a .bbl."""
    folder = Path(paper_dir)
    found = {"paper_dir": paper_dir, "arxiv_id": folder.name.replace("-", ".", 1), "versions": [], "error": ""}
    try:
        data = json.loads((folder / "versions.json").read_text(encoding="utf-8"))
        found["arxiv_id"] = data.get("arxiv_id") or found["arxiv_id"]
        for version, record in data.get("versions", {}).items():
            if (record or {}).get("status") == SUCCESS and gather(folder / version, SUCCESS).needs_bbl:
                found["versions"].append(version)
    except Exception as exc:  # one paper never stops the scan
        found["error"] = f"{type(exc).__name__}: {exc}"
    return found


def settled_versions(log_path: Path) -> set[tuple[str, str]]:
    """Versions already asked whose source has no .bbl or no longer exists."""
    latest: dict[tuple[str, str], str] = {}
    if log_path.is_file():
        with open(log_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                latest[(row["arxiv_id"], row["version"])] = row["status"]
    return {key for key, status in latest.items() if status in (NO_BBL, GONE)}


def fetch_version(arxiv_id: str, version: str, paper_dir: Path, layout: DataLayout, session, limiter) -> dict:
    """Download one version and add its .bbl files to the corpus."""
    versioned_id = f"{arxiv_id}{version}"
    scratch = layout.work / "downloads"
    scratch.mkdir(parents=True, exist_ok=True)
    part_file = scratch / f"{paper_dir.name}{version}.bbl.part"
    tmp_dir = scratch / f"{paper_dir.name}{version}.bbl.tmp"
    outcome = {"status": FAILED, "bbl_files": 0, "bbl_bytes": 0, "http_status": "", "error": ""}
    try:
        download = fetch_source(session, versioned_id, part_file, limiter=limiter)
        outcome["http_status"] = download.http_status or ""
        if not download.ok:
            outcome.update(status=GONE if download.http_status == 404 else FAILED, error=download.error)
            return outcome
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True)
        try:
            result = extract_source(part_file, tmp_dir, fallback_name=f"{paper_dir.name}{version}.tex")
        except (IncompleteSourceError, CorruptSourceError) as exc:
            outcome["error"] = str(exc)
            return outcome
        folder = paper_dir / version
        added = []
        for name in result.kept_files:
            target = folder / name
            if name.lower().endswith(".bbl") and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tmp_dir / name, target)
                added.append(name)
        if not added:
            outcome["status"] = NO_BBL
            return outcome
        # versions.json only after the files are in place, as for a download.
        # Written directly: VersionStore.update() would count these requests as a new download.
        store = VersionStore(paper_dir, arxiv_id)
        record = store.data["versions"][version]
        size = sum((folder / name).stat().st_size for name in added)
        record["kept_files"] = sorted(record.get("kept_files", []) + added)
        record["kept_bytes"] = int(record.get("kept_bytes") or 0) + size
        record["bbl_added_at"] = now_iso()
        atomic_write_json(store.path, store.data)
        outcome.update(status=ADDED, bbl_files=len(added), bbl_bytes=size)
        return outcome
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        part_file.unlink(missing_ok=True)


def run(todo: list[dict], layout: DataLayout, *, session=None, limiter=ARXIV_EXPORT,
        workers: int = DOWNLOAD_WORKERS) -> dict[str, int]:
    """Fetch the versions of `todo` (one thread per paper at a time, so two
    threads never write one versions.json) and return the counts per status."""
    session = session or make_session()
    log = CsvLog(layout.logs / "bbl_log.csv", LOG_FIELDS)
    counts: dict[str, int] = {}

    def fetch_paper(paper: dict) -> None:
        for version in paper["versions"]:
            outcome = fetch_version(paper["arxiv_id"], version, Path(paper["paper_dir"]), layout, session, limiter)
            log.write(time=now_iso(), arxiv_id=paper["arxiv_id"], version=version, **outcome)
            counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
            print(f"[bbl] {paper['arxiv_id']}{version}: {outcome['status']} {outcome['error']}".rstrip(), flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(fetch_paper, todo):  # an error such as a full disk stops the run here
            pass
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Add the .bbl files of versions without references")
    parser.add_argument("--data-dir", default="data", help="same folder as main.py --data-dir")
    parser.add_argument("--ids", nargs="+", help="only these papers (2604.05535 or 2604-05535)")
    parser.add_argument("--limit", type=int, help="only the first N papers that need a .bbl")
    parser.add_argument("--dry-run", action="store_true", help="count the versions, send no request")
    parser.add_argument("--retry-no-bbl", action="store_true",
                        help="also ask versions whose source had no .bbl or was not found")
    parser.add_argument("--workers", type=int, default=DOWNLOAD_WORKERS, help="papers downloaded at a time")
    parser.add_argument("--scan-workers", type=int, default=os.cpu_count() or 1, help="processes of the scan")
    parser.add_argument("--force", action="store_true", help="start even if work/crawler.lock looks fresh")
    args = parser.parse_args(argv)

    layout = DataLayout(args.data_dir)
    if not layout.corpus.is_dir():
        print(f"No corpus in {layout.corpus}")
        return 2
    folders = sorted(str(p) for p in layout.corpus.iterdir() if p.is_dir())
    if args.ids:
        wanted = {i.replace(".", "-", 1) for i in args.ids}
        folders = [f for f in folders if Path(f).name in wanted]
    with ProcessPoolExecutor(max_workers=max(1, args.scan_workers)) as pool:
        scanned = list(pool.map(scan_paper, folders, chunksize=16))
    for paper in scanned:
        if paper["error"]:
            print(f"[bbl] {paper['arxiv_id']}: not scanned, {paper['error']}")

    settled = set() if args.retry_no_bbl else settled_versions(layout.logs / "bbl_log.csv")
    todo, already = [], 0
    for paper in scanned:
        versions = [v for v in paper["versions"] if (paper["arxiv_id"], v) not in settled]
        already += len(paper["versions"]) - len(versions)
        if versions:
            todo.append(dict(paper, versions=versions))
    todo = todo[:args.limit] if args.limit is not None else todo
    print(f"[bbl] {len(folders)} papers scanned: {sum(len(p['versions']) for p in todo)} versions of {len(todo)} "
          f"papers to ask, {already} already known to have no .bbl")
    if args.dry_run or not todo:
        return 0

    layout.create()
    lock = RunLock(layout.work / "crawler.lock")
    try:
        lock.acquire(force=args.force)
    except RuntimeError as exc:
        print(exc)
        return 2
    try:
        counts = run(todo, layout, workers=max(1, args.workers))
    finally:
        lock.release()
    print("[bbl] done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("[bbl] next: python src/pipeline_m2.py --data-dir " + str(args.data_dir) + " --force")
    return 1 if counts.get(FAILED) else 0


if __name__ == "__main__":
    sys.exit(main())
