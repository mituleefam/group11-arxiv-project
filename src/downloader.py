"""Member 2 - Source download.

Downloads the source of every version of every paper found by discovery and
turns it into corpus/<yymm-id>/<version>/ with only the .tex/.bib files:

    corpus/2604-05535/
        metadata.json        (metadata.py)
        versions.json        state of every version (checkpoint.py)
        v1/main.tex          (extractor.py)
        v1/sections/intro.tex
        v1/refs.bib
        v2/                  empty folder: v2 is pdf_only or no_tex

Each version is fetched on its own from https://export.arxiv.org/src/<id>v<N>,
through the shared rate_limiter.ARXIV (3 s between requests, one connection at
a time, shared with discovery and metadata).

Steps for one version (process_version):
  1. download into work/downloads/<yymm-id><vN>.part, streamed to disk;
  2. check the HTTP status and that the body has the announced Content-Length;
  3. extract into the temporary folder corpus/<yymm-id>/.<vN>.tmp/ (extractor.py);
  4. check the written files, then rename the temporary folder to <vN>/;
  5. record the result in versions.json, then delete the downloaded file.
A crash at any step leaves only .part files and .tmp folders, which the next
run deletes and redoes; a finished version is never downloaded or written again.

Every version also gets one row in logs/download_log.csv for Member 4
(time, sizes, attempts, status, error).

Called by main.py as run(papers, layout); can also run alone:

    python src/downloader.py --data-dir data                 # every paper of the manifest
    python src/downloader.py --data-dir data --ids 2604.05535 2604.05536
    python src/downloader.py --data-dir data --limit 10      # trial run
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from urllib3.exceptions import HTTPError as Urllib3Error

from checkpoint import VersionStore
from common import FAILED, NOT_FOUND, SUCCESS, CsvLog, DataLayout, Manifest, now_iso
from extractor import CorruptSourceError, IncompleteSourceError, extract_source
from rate_limiter import ARXIV, RETRY_STATUS, RateLimiter, _retry_after_seconds, make_session

SRC_URL = "https://export.arxiv.org/src/{versioned_id}"

CONNECT_TIMEOUT = 15   # seconds to open the connection
READ_TIMEOUT = 120     # seconds without receiving any byte before giving up
CHUNK = 1024 * 1024    # stream the body 1 MB at a time
MAX_HTTP_ATTEMPTS = 5  # per download: failed requests in a row with no new byte (backoff 3, 6, 12, 24 s)
FIRST_BACKOFF = 3.0    # seconds before the first retry, doubled after each one
MAX_REQUESTS = 30      # per download, counting the requests that resumed a broken body
MAX_DOWNLOADS = 3      # per version and run: downloads of a body that turned out cut off
# Stop the stage when this many versions in a row fail: arXiv is probably
# blocking us (403) or unreachable, and going on would only add failures.
MAX_FAILED_IN_A_ROW = 10

# Columns of logs/download_log.csv: one row per processed version.
DOWNLOAD_LOG_FIELDS = [
    "time", "arxiv_id", "version", "status", "source_format", "http_status",
    "attempts", "download_seconds", "extract_seconds", "total_seconds",
    "downloaded_bytes", "original_bytes", "image_bytes", "kept_bytes",
    "kept_files", "tex_files", "bib_files", "skipped_files", "error",
]


@dataclass
class Download:
    """Result of fetch_source().

    ok            True when the whole body was saved with HTTP 200
    http_status   last HTTP status received (None after a network error)
    size          bytes saved on disk (the compressed size)
    attempts      HTTP requests made
    seconds       wall time spent, waits included
    error         "" when ok, otherwise what went wrong
    """

    ok: bool
    http_status: int | None
    size: int
    attempts: int
    seconds: float
    error: str = ""


def _content_range_start_total(header: str | None) -> tuple[int | None, int | None]:
    """Parse a Content-Range header such as "bytes 12582912-19714924/19714925".

    Returns (first byte, full size); either is None when it is missing or
    unreadable ("*" as the size means the server does not know it).
    """
    if not header:
        return None, None
    try:
        unit, spec = header.split(" ", 1)
        span, full = spec.split("/", 1)
        first = int(span.split("-", 1)[0]) if unit == "bytes" and span != "*" else None
        return first, int(full) if full.strip().isdigit() else None
    except ValueError:
        return None, None


def fetch_source(session: requests.Session, versioned_id: str, target: Path, *,
                 limiter: RateLimiter = ARXIV,
                 max_attempts: int = MAX_HTTP_ATTEMPTS) -> Download:
    """Download the source of one version (e.g. "2604.05535v2") into `target`.

    The body is streamed to disk while the limiter is held, so only one
    connection to arXiv is open and the RAM use does not depend on the file
    size. The bytes are saved exactly as sent (no automatic gunzip), so
    extractor.py sees the real format and the size is the real download size.

    Resuming: when the connection breaks in the middle of the body, the next
    request asks only for the missing part (HTTP `Range: bytes=<saved>-`)
    and appends it, instead of downloading the whole file again. `If-Range`
    carries the ETag / Last-Modified of the first answer, so if the file
    changed on the server in between, the server sends the whole new file
    (HTTP 200) and the download restarts from zero. A server that ignores
    Range also answers 200, which simply restarts the download. Each of
    these requests goes through the limiter like any other.

    Retries: network errors, a body shorter than its Content-Length, HTTP 429
    and 5xx are retried with a doubling backoff (or the server's Retry-After).
    `max_attempts` counts the requests that brought no new byte; a request
    that made progress does not use one up (at most MAX_REQUESTS requests in
    total). Other statuses such as 404 or 403 are returned at once.
    """
    url = SRC_URL.format(versioned_id=versioned_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()  # never resume from a file left by an earlier run
    started = time.perf_counter()
    backoff = FIRST_BACKOFF
    status, size, error = None, 0, ""
    total: int | None = None      # full size of the file, once known
    validator: str | None = None  # ETag or Last-Modified of the first answer
    requests_made, without_progress = 0, 0

    while requests_made < MAX_REQUESTS and without_progress < max_attempts:
        requests_made += 1
        retry_after, before, error, status = 0.0, size, "", None
        headers = {}
        if size > 0:
            headers["Range"] = f"bytes={size}-"
            if validator:
                headers["If-Range"] = validator
        with limiter:
            try:
                with session.get(url, stream=True, headers=headers,
                                 timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as response:
                    status = response.status_code
                    retry_after = _retry_after_seconds(response)
                    mode = None
                    if status == 200:  # whole file (first request, or Range not honoured)
                        size, mode = 0, "wb"
                        length = response.headers.get("Content-Length", "")
                        total = int(length) if length.isdigit() else None
                        validator = response.headers.get("ETag") or response.headers.get("Last-Modified")
                    elif status == 206 and size > 0:  # the missing part only
                        first, full = _content_range_start_total(response.headers.get("Content-Range"))
                        if first == size:
                            mode = "ab"
                            total = full if full is not None else total
                        else:
                            error = f"unexpected Content-Range {response.headers.get('Content-Range')!r}"
                    elif status == 416:  # range not satisfiable: start again from zero
                        error = "HTTP 416"
                    else:
                        error = f"HTTP {status}"
                    if mode is not None:
                        with open(target, mode) as f:
                            # decode_content=False: keep gzip bytes as they are.
                            for chunk in response.raw.stream(CHUNK, decode_content=False):
                                f.write(chunk)
                                size += len(chunk)
            except (requests.RequestException, Urllib3Error, OSError) as exc:
                # Errors while streaming response.raw come straight from urllib3.
                error = f"{type(exc).__name__}: {exc}"

        if not error:
            if total is not None and size < total:
                error = f"incomplete body: {size} of {total} bytes"
            elif total is not None and size > total:
                error, size = f"body longer than announced: {size} of {total} bytes", 0
            elif size == 0:
                error = "empty body"
            else:
                return Download(True, status, size, requests_made,
                                round(time.perf_counter() - started, 3))
        if status == 416 or error.startswith("unexpected Content-Range"):
            size, total, validator = 0, None, None  # resuming failed: next request from zero
        if status is not None and status not in (200, 206, 416) and status not in RETRY_STATUS:
            break  # 404, 403...: a new request would get the same answer
        if size > before:
            without_progress, backoff = 0, FIRST_BACKOFF  # progress: resume right away
            continue
        without_progress += 1
        if without_progress < max_attempts:
            time.sleep(max(backoff, retry_after))
            backoff *= 2

    return Download(False, status, size, requests_made,
                    round(time.perf_counter() - started, 3), error)


def _remove(path: Path) -> None:
    """Delete a file or folder if it exists (leftovers of earlier attempts)."""
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _verify(folder: Path, kept_files: list[str]) -> str:
    """Check that every file reported by the extractor is really in `folder`.
    Returns "" when everything is in place, otherwise the problem."""
    for name in kept_files:
        path = folder / name
        if not path.is_file():
            return f"missing after extraction: {name}"
    return ""


def process_version(paper: dict, version: str, layout: DataLayout, session: requests.Session,
                    store: VersionStore, log: CsvLog, *,
                    limiter: RateLimiter = ARXIV) -> dict:
    """Download, extract and record one version of one paper.

    `paper` is a manifest record (arxiv_id, folder, versions...), `version` is
    "v1", "v2"... Returns the record saved in versions.json, whose "status"
    is success, pdf_only, no_tex, not_found (HTTP 404) or failed.
    """
    arxiv_id = paper["arxiv_id"]
    versioned_id = f"{arxiv_id}{version}"
    paper_dir = layout.paper_dir(arxiv_id)
    final_dir = paper_dir / version
    tmp_dir = paper_dir / f".{version}.tmp"
    part_file = layout.work / "downloads" / f"{paper['folder']}{version}.part"
    started = time.perf_counter()

    record = {"status": FAILED, "source_format": None, "http_status": None, "attempts": 0,
              "downloaded_bytes": 0, "original_bytes": 0, "image_bytes": 0, "kept_bytes": 0,
              "kept_files": [], "skipped": [], "download_seconds": 0.0,
              "extract_seconds": 0.0, "error": ""}

    try:
        for _ in range(MAX_DOWNLOADS):
            download = fetch_source(session, versioned_id, part_file, limiter=limiter)
            record["attempts"] += download.attempts
            record["download_seconds"] = round(record["download_seconds"] + download.seconds, 3)
            record.update(http_status=download.http_status, downloaded_bytes=download.size)
            if not download.ok:
                record["status"] = NOT_FOUND if download.http_status == 404 else FAILED
                record["error"] = download.error
                break

            _remove(tmp_dir)
            tmp_dir.mkdir(parents=True)
            extract_started = time.perf_counter()
            try:
                result = extract_source(part_file, tmp_dir,
                                        fallback_name=f"{paper['folder']}{version}.tex")
            except IncompleteSourceError as exc:
                record["error"] = f"incomplete source: {exc}"
                continue  # download the version again
            except CorruptSourceError as exc:
                record.update(status=FAILED, error=f"corrupt source: {exc}")
                break
            finally:
                record["extract_seconds"] = round(
                    record["extract_seconds"] + time.perf_counter() - extract_started, 3)

            problem = _verify(tmp_dir, result.kept_files)
            if problem:
                record.update(status=FAILED, error=problem)
                break

            # The version is complete: move it into place, replacing only a
            # leftover folder that versions.json does not mark as finished.
            _remove(final_dir)
            tmp_dir.rename(final_dir)
            record.update(status=result.status, source_format=result.source_format,
                          original_bytes=result.original_bytes, image_bytes=result.image_bytes,
                          kept_bytes=result.kept_bytes, kept_files=result.kept_files,
                          skipped=result.skipped, error="")
            break
        else:
            record["status"] = FAILED  # every download was cut off
    finally:
        _remove(tmp_dir)
        _remove(part_file)

    record["total_seconds"] = round(time.perf_counter() - started, 3)
    store.update(version, record)
    log.write(time=now_iso(), arxiv_id=arxiv_id, version=version, status=record["status"],
              source_format=record["source_format"] or "", http_status=record["http_status"] or "",
              attempts=record["attempts"], download_seconds=record["download_seconds"],
              extract_seconds=record["extract_seconds"], total_seconds=record["total_seconds"],
              downloaded_bytes=record["downloaded_bytes"], original_bytes=record["original_bytes"],
              image_bytes=record["image_bytes"], kept_bytes=record["kept_bytes"],
              kept_files=len(record["kept_files"]),
              tex_files=sum(1 for n in record["kept_files"] if n.lower().endswith(".tex")),
              bib_files=sum(1 for n in record["kept_files"] if n.lower().endswith(".bib")),
              skipped_files=len(record["skipped"]), error=record["error"])
    return record


def run(papers: list[dict], layout: DataLayout, *, session: requests.Session | None = None,
        limiter: RateLimiter = ARXIV) -> dict[str, int]:
    """Download every version of every paper in `papers` (main.py contract).

    `papers` are manifest records with status "success". Versions already
    final in versions.json are skipped, so the function can be called again
    after an interruption. Returns the number of versions per status
    processed in this call (plus "skipped" for those already done).
    """
    session = session or make_session()
    log = CsvLog(layout.logs / "download_log.csv", DOWNLOAD_LOG_FIELDS)
    todo = []
    counts: dict[str, int] = {"skipped": 0}
    for paper in papers:
        store = VersionStore(layout.paper_dir(paper["arxiv_id"]), paper["arxiv_id"])
        for version in paper["versions"]:
            if store.is_final(version):
                counts["skipped"] += 1
            else:
                todo.append((paper, version, store))

    print(f"[download] {len(todo)} versions to process, {counts['skipped']} already done")
    failed_in_a_row = 0
    started = time.perf_counter()
    for done, (paper, version, store) in enumerate(todo, start=1):
        record = process_version(paper, version, layout, session, store, log, limiter=limiter)
        status = record["status"]
        counts[status] = counts.get(status, 0) + 1
        if status == FAILED:
            failed_in_a_row += 1
            print(f"[download] {paper['arxiv_id']}{version} failed: {record['error']}")
            if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                raise RuntimeError(f"{failed_in_a_row} versions failed in a row, last error: "
                                   f"{record['error']}; re-run later to resume")
        else:
            failed_in_a_row = 0
        if done % 50 == 0 or done == len(todo):
            elapsed = time.perf_counter() - started
            left = elapsed / done * (len(todo) - done)
            summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"[download] {done}/{len(todo)} | {summary} | about {left / 60:.0f} min left")
    return counts


def _papers_from_manifest(layout: DataLayout, ids: list[str] | None,
                          limit: int | None) -> list[dict]:
    """Successful manifest records, optionally only `ids` and the first `limit`."""
    records = [r for r in Manifest(layout.manifest).load().values() if r["status"] == SUCCESS]
    records.sort(key=lambda r: r["arxiv_id"])
    if ids:
        wanted = set(ids)
        records = [r for r in records if r["arxiv_id"] in wanted]
    return records[:limit] if limit is not None else records


def main(argv=None) -> int:
    """Command-line entry point to run only this stage (see module docstring)."""
    parser = argparse.ArgumentParser(description="Download and extract arXiv sources")
    parser.add_argument("--data-dir", default="data", help="same folder as main.py --data-dir")
    parser.add_argument("--ids", nargs="+", help="only these arXiv IDs (must be in the manifest)")
    parser.add_argument("--limit", type=int, help="only the first N papers")
    args = parser.parse_args(argv)

    layout = DataLayout(args.data_dir)
    layout.create()
    papers = _papers_from_manifest(layout, args.ids, args.limit)
    if not papers:
        print("No paper in the manifest: run the discovery stage of main.py first.")
        return 1
    counts = run(papers, layout)
    print("[download] done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
