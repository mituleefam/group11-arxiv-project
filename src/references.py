"""Member 3 - References crawling (Semantic Scholar Graph API).

For every paper found by discovery, writes in corpus/<yymm-id>/:

    references_all.json  every reference Semantic Scholar returns (working data)
        {"arxiv_id", "status", "s2_paper_id", "s2_reference_count", "pages",
         "crawled_at", "arxiv_dates_complete",
         "references": [{"ref_arxiv_id", "ref_title", "ref_authors",
                         "ref_submission_date", "ref_s2_id", "ref_publication_date",
                         "ref_year", "ref_venue", "ref_external_ids", "metadata_status"}]}

    references.json      only the references with an arXiv ID, keyed by the
                         cited ID in "yymm-id" form (old-style IDs such as
                         "hep-th/9901001" unchanged):
        {"1706-03762": {"title", "authors", "submission_date", "semantic_scholar_id"}}

Paper status:
    success      references were returned
    empty        Semantic Scholar knows the paper but lists no reference
    unavailable  it reports references (referenceCount > 0) but returns none
    not_found    the paper is not indexed by Semantic Scholar
    api_error    the API kept failing; nothing is written, the next run retries

Reference metadata_status: "unresolved" (Semantic Scholar could not link the
citation to a paper), "incomplete" (title or authors missing) or "complete".

`ref_submission_date` is the arXiv v1 date of the cited paper, asked from the
arXiv API; Semantic Scholar's publicationDate (usually the journal date) is
kept apart as `ref_publication_date`. These arXiv calls run in a background
thread (DateWorker): they share rate_limiter.ARXIV with the source download,
which holds it during each file transfer, and must not stall this crawl.

API key: set S2_API_KEY in the environment, never in the repository.
Usage: main.py calls run(papers, layout); standalone options are in parse_args.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import requests

from common import (API_ERROR, NOT_FOUND, SUCCESS, CsvLog, DataLayout, Manifest,
                    atomic_write_json, now_iso)
from discovery import API_LOG_FIELDS, id_key, query_batch
from rate_limiter import (ARXIV, RETRY_STATUS, RateLimiter, _retry_after_seconds,
                          make_session)

S2_API = "https://api.semanticscholar.org/graph/v1"
# Fields of each cited paper, requested flat (nested forms like authors.name are rejected).
REFERENCE_FIELDS = "paperId,title,authors,externalIds,publicationDate,year,venue"
PAGE_SIZE = 1000     # maximum `limit` of /paper/{id}/references
MAX_PAGES = 10       # Semantic Scholar stops paging at offset 10 000
LOOKUP_BATCH = 500   # maximum number of IDs per /paper/batch call
ARXIV_BATCH = 100    # IDs per arXiv API call when resolving submission dates
MAX_FAILED_IN_A_ROW = 5  # the API is probably unreachable: stop

EMPTY, UNAVAILABLE = "empty", "unavailable"
FINAL_STATUSES = {SUCCESS, EMPTY, UNAVAILABLE, NOT_FOUND}
COMPLETE, INCOMPLETE, UNRESOLVED = "complete", "incomplete", "unresolved"

# Columns of logs/references_log.csv: one row per paper and attempt.
LOG_FIELDS = ["time", "arxiv_id", "status", "s2_paper_id", "s2_reference_count",
              "n_references", "n_with_arxiv_id", "n_unique_arxiv_id", "n_complete",
              "n_incomplete", "n_unresolved", "pages", "attempts", "seconds", "error"]


# ---------------------------------------------------------------------------
# arXiv identifiers of cited papers
# ---------------------------------------------------------------------------

NEW_ID = re.compile(r"(\d{2})(\d{2})\.(\d{4,5})")
# Before April 2007: archive[.SUBJECT-CLASS]/YYMMNNN, e.g. hep-th/9901001, math.GT/0309136
OLD_ID = re.compile(r"[a-z][a-z\-]*(\.[A-Za-z\-]+)?/\d{2}(\d{2})\d{3}")


def normalize_arxiv_id(value) -> str | None:
    """Clean an externalIds.ArXiv value (drop 'arXiv:' and a version suffix).
    Returns None unless it is a valid modern or old-style ID; old-style IDs
    are kept as given, as the statement asks."""
    if not value:
        return None
    text = re.sub(r"^arxiv:", "", str(value).strip(), flags=re.IGNORECASE)
    text = re.sub(r"v\d+$", "", text)
    if match := NEW_ID.fullmatch(text):
        yymm, width = int(match[1] + match[2]), len(match[3])
        # 4-digit numbers from 0704 to 1412, 5-digit numbers from 1501.
        valid = (704 <= yymm <= 1412 and width == 4) or (yymm >= 1501 and width == 5)
        return text if valid and 1 <= int(match[2]) <= 12 else None
    if (match := OLD_ID.fullmatch(text)) and 1 <= int(match[2]) <= 12:
        return text
    return None


def arxiv_lookup_key(arxiv_id: str) -> str:
    """ID as the arXiv API reports it: 'math.GT/0309136' -> 'math/0309136'."""
    return re.sub(r"^([a-z\-]+)\.[A-Za-z\-]+/", r"\1/", arxiv_id)


# ---------------------------------------------------------------------------
# Semantic Scholar client
# ---------------------------------------------------------------------------

class SemanticScholar:
    """Graph API client: one request per 1.1 s with an API key, one per 3.1 s
    without (100 requests per 5 minutes).

    HTTP 429 from Semantic Scholar mostly reflects load on their side, not our
    pace: in a 500-paper test, slowing the gap down to ~9 s left the share of
    throttled papers unchanged (and doubled the run time), while long
    exponential waits (5 to 80 s) cost ~68 of 79 minutes. So a 429 is retried
    after a short fixed wait (or the server's Retry-After), keeping the normal
    pace. Network errors and 5xx keep an exponential backoff.
    """

    RETRY_429_WAIT = 2.0   # seconds before retrying after HTTP 429
    MAX_429_RETRIES = 12   # 429 answers tolerated for one request

    def __init__(self, api_key: str | None = None, *, session=None,
                 limiter: RateLimiter | None = None, max_attempts: int = 6):
        self.session = session if session is not None else make_session()
        self.has_key = bool(api_key)
        if api_key:
            self.session.headers["x-api-key"] = api_key
        self.limiter = limiter or RateLimiter(1.1 if api_key else 3.1)
        self.max_attempts = max_attempts
        self.throttled = 0  # number of HTTP 429 answers, for the summary line

    def call(self, method: str, path: str, *, params=None, payload=None):
        """One API call. HTTP 429 is retried after RETRY_429_WAIT (up to
        MAX_429_RETRIES times); network errors and 5xx after 5, 10, 20, 40, 80 s
        (up to max_attempts). Returns (data, attempts, error, http_status);
        `error` is "" on success."""
        backoff, error, status = 5.0, "", None
        attempts = failures = throttled = 0
        while True:
            attempts += 1
            response = None
            with self.limiter:
                try:
                    response = self.session.request(method, f"{S2_API}/{path}", params=params,
                                                    json=payload, timeout=60)
                    status = response.status_code
                    if status == 200:
                        return response.json(), attempts, "", status
                    error = f"HTTP {status}"
                except requests.RequestException as exc:  # also invalid JSON in a 200
                    error = f"{type(exc).__name__}: {exc}"
                    status = response.status_code if response is not None else None
            if status in (401, 403):
                raise RuntimeError(f"Semantic Scholar answered {error}; check S2_API_KEY")
            if status == 429:
                self.throttled += 1
                throttled += 1
                if throttled > self.MAX_429_RETRIES:
                    return None, attempts, error, status
                time.sleep(max(self.RETRY_429_WAIT, _retry_after_seconds(response)))
                continue
            if status is not None and status != 200 and status not in RETRY_STATUS:
                return None, attempts, error, status  # e.g. 404: retrying will not help
            failures += 1
            if failures >= self.max_attempts:
                return None, attempts, error, status
            time.sleep(max(backoff, _retry_after_seconds(response)))
            backoff = min(backoff * 2, 120)

    def lookup(self, arxiv_ids: list[str]):
        """Look up to 500 papers in one call. The batch endpoint answers in
        request order, with null for unknown papers. Returns
        ({arxiv_id: {"paperId", "referenceCount"} | None}, attempts, error)."""
        data, attempts, error, _ = self.call(
            "POST", "paper/batch", params={"fields": "paperId,referenceCount"},
            payload={"ids": [f"arXiv:{i}" for i in arxiv_ids]})
        if not error and (not isinstance(data, list) or len(data) != len(arxiv_ids)):
            error = "unexpected /paper/batch response"
        return (None if error else dict(zip(arxiv_ids, data))), attempts, error

    def references(self, paper_id: str):
        """Every reference of a paper, page by page until no `next` is left.
        A failed page fails the whole paper, so a partial list is never saved.
        Returns (items, pages, attempts, error, status)."""
        items, offset, pages, attempts_total = [], 0, 0, 0
        while pages < MAX_PAGES:
            data, attempts, error, status = self.call(
                "GET", f"paper/{paper_id}/references",
                params={"fields": REFERENCE_FIELDS, "offset": offset, "limit": PAGE_SIZE})
            attempts_total += attempts
            if error:
                return None, pages, attempts_total, error, status
            pages += 1
            page = data.get("data") or []
            items.extend(page)
            if data.get("next") is None or not page:
                break
            offset = data["next"]
        return items, pages, attempts_total, "", 200


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _clean(text) -> str | None:
    return " ".join(str(text).split()) or None if text else None


def normalize_reference(item: dict | None) -> dict:
    """One /references entry ({"citedPaper": {...}}) in our format. Missing
    values stay null: nothing is guessed."""
    cited = (item or {}).get("citedPaper") or {}
    external = cited.get("externalIds") or {}
    title = _clean(cited.get("title"))
    authors = [n for n in (_clean(a.get("name")) for a in cited.get("authors") or [] if a) if n]
    paper_id = cited.get("paperId")
    return {
        "ref_arxiv_id": normalize_arxiv_id(external.get("ArXiv")),
        "ref_title": title,
        "ref_authors": authors,
        "ref_submission_date": None,  # filled from the arXiv API, see ArxivDates
        "ref_s2_id": paper_id,
        "ref_publication_date": cited.get("publicationDate"),  # journal/venue date, not arXiv
        "ref_year": cited.get("year"),
        "ref_venue": _clean(cited.get("venue")),
        "ref_external_ids": external,
        "metadata_status": (UNRESOLVED if not paper_id else
                            INCOMPLETE if not (title and authors) else COMPLETE),
    }


def references_dict(doc: dict) -> dict:
    """Content of references.json (the first entry wins when a paper is listed twice)."""
    out: dict[str, dict] = {}
    for ref in doc["references"]:
        if ref_id := ref["ref_arxiv_id"]:
            key = ref_id if "/" in ref_id else ref_id.replace(".", "-")
            out.setdefault(key, {"title": ref["ref_title"], "authors": ref["ref_authors"],
                                 "submission_date": ref["ref_submission_date"],
                                 "semantic_scholar_id": ref["ref_s2_id"]})
    return out


# ---------------------------------------------------------------------------
# arXiv submission dates of cited papers
# ---------------------------------------------------------------------------

class ArxivDates:
    """Cache of arXiv v1 dates in work/ref_arxiv_dates.jsonl, one
    {"id", "date"} line per ID (date null: arXiv has no such paper).

    Append-only, so a batch adds ~100 short lines instead of rewriting a large
    file on Google Drive. IDs whose lookup failed are not cached and are asked
    again later. A cache in the older ref_arxiv_dates.json is still read.
    """

    def __init__(self, path: Path, session, log: CsvLog, *,
                 limiter: RateLimiter = ARXIV, query=None):
        self.path, self.log = Path(path), log
        self.query = query or (lambda ids: query_batch(session, ids, limiter))
        self.cache: dict[str, str | None] = {}
        legacy = self.path.with_suffix(".json")
        if legacy.exists():
            try:
                self.cache.update(json.loads(legacy.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                    self.cache[item["id"]] = item["date"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue  # line cut by a crash

    def known(self, arxiv_id: str) -> bool:
        return arxiv_lookup_key(arxiv_id) in self.cache

    def resolve(self, arxiv_ids) -> bool:
        """Ask arXiv about every ID not cached yet, 100 per call. arXiv simply
        leaves out IDs it does not know, so they are cached as null. Returns
        False when a call failed (the rest of the round is skipped)."""
        todo = sorted({arxiv_lookup_key(i) for i in arxiv_ids} - set(self.cache))
        for start in range(0, len(todo), ARXIV_BATCH):
            batch = todo[start:start + ARXIV_BATCH]
            started = time.perf_counter()
            entries, attempts, error = self.query(batch)
            self.log.write(time=now_iso(), stage="ref_dates", first_id=batch[0],
                           last_id=batch[-1], requested=len(batch), returned=len(entries),
                           attempts=attempts, seconds=round(time.perf_counter() - started, 3),
                           error=error)
            if error:
                # arXiv is failing (often HTTP 429 on Colab): stop this round instead of
                # trying every remaining batch, otherwise each round would retry all
                # missing IDs and the waiting would grow quadratically. Uncached IDs
                # are asked again in a later round or on the next run.
                return False
            found = {e["arxiv_id"]: e["published"] or None for e in entries}
            answers = {i: found.get(i) for i in batch}
            with open(self.path, "a", encoding="utf-8") as f:
                f.writelines(json.dumps({"id": i, "date": d}) + "\n" for i, d in answers.items())
            self.cache.update(answers)
        return True

    def fill(self, doc: dict) -> bool:
        """Copy cached dates into a references_all document. Returns True when
        every arXiv reference has an answer."""
        complete = True
        for ref in doc["references"]:
            if ref["ref_arxiv_id"]:
                if self.known(ref["ref_arxiv_id"]):
                    ref["ref_submission_date"] = self.cache[arxiv_lookup_key(ref["ref_arxiv_id"])]
                else:
                    complete = False
        doc["arxiv_dates_complete"] = complete
        return complete


class DateWorker(threading.Thread):
    """Background thread that fills the arXiv dates of crawled papers.

    The crawl loop saves each paper and hands over those with uncached cited
    IDs; once 100 IDs are waiting this thread asks arXiv and saves the papers
    again. Papers still incomplete stay arxiv_dates_complete=false on disk
    and are picked up by the next run.
    """

    def __init__(self, layout: DataLayout, dates: ArxivDates):
        super().__init__(name="arxiv-dates", daemon=True)
        self.layout, self.dates = layout, dates
        self.inbox: queue.Queue = queue.Queue()
        self.pending: dict[str, dict] = {}  # citing arxiv_id -> references_all document
        self.flush_at = ARXIV_BATCH
        self.pause_until = 0.0  # after an arXiv failure, wait before the next round
        self.error: BaseException | None = None

    def _missing_ids(self) -> set[str]:
        return {r["ref_arxiv_id"] for doc in self.pending.values() for r in doc["references"]
                if r["ref_arxiv_id"] and not self.dates.known(r["ref_arxiv_id"])}

    RETRY_PAUSE = 600  # seconds to leave arXiv alone after a failed round

    def _flush(self) -> None:
        if not self.dates.resolve(self._missing_ids()):
            self.pause_until = time.monotonic() + self.RETRY_PAUSE
        for arxiv_id, doc in list(self.pending.items()):
            if self.dates.fill(doc):
                del self.pending[arxiv_id]
                save(self.layout, doc)
        # If arXiv failed, wait for a full new batch before asking again.
        self.flush_at = len(self._missing_ids()) + ARXIV_BATCH

    def run(self) -> None:
        try:
            while (doc := self.inbox.get()) is not None:
                self.pending[doc["arxiv_id"]] = doc
                if (time.monotonic() >= self.pause_until
                        and len(self._missing_ids()) >= self.flush_at):
                    self._flush()
            self._flush()
        except BaseException as exc:  # re-raised in the crawl thread by finish()
            self.error = exc

    def finish(self) -> None:
        """Resolve what is left, then stop."""
        self.inbox.put(None)
        self.join()
        if self.error is not None:
            raise self.error


# ---------------------------------------------------------------------------
# Crawling
# ---------------------------------------------------------------------------

def crawl_paper(client: SemanticScholar, arxiv_id: str, s2_paper: dict | None):
    """Fetch and normalize the references of one paper. Returns
    (doc, attempts, error); doc is None when the API failed (api_error)."""
    doc = {"arxiv_id": arxiv_id, "status": NOT_FOUND, "s2_paper_id": None,
           "s2_reference_count": None, "pages": 0, "crawled_at": now_iso(),
           "arxiv_dates_complete": True, "references": []}
    if s2_paper is None:
        return doc, 0, ""
    doc["s2_paper_id"] = s2_paper.get("paperId")
    doc["s2_reference_count"] = count = s2_paper.get("referenceCount")
    if count == 0:
        doc["status"] = EMPTY
        return doc, 0, ""

    items, doc["pages"], attempts, error, status = client.references(doc["s2_paper_id"])
    if error:
        return (doc if status == 404 else None), attempts, error
    doc["references"] = [normalize_reference(item) for item in items]
    doc["status"] = SUCCESS if items else UNAVAILABLE if (count or 0) > 0 else EMPTY
    return doc, attempts, ""


def save(layout: DataLayout, doc: dict) -> None:
    """references_all.json is written last: it marks the paper as finished."""
    folder = layout.paper_dir(doc["arxiv_id"])
    atomic_write_json(folder / "references.json", references_dict(doc))
    atomic_write_json(folder / "references_all.json", doc)


def load_doc(layout: DataLayout, arxiv_id: str) -> dict | None:
    try:
        return json.loads((layout.paper_dir(arxiv_id) / "references_all.json")
                          .read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None  # missing or damaged: crawl again


def log_paper(log: CsvLog, arxiv_id: str, status: str, doc: dict | None,
              attempts: int, seconds: float, error: str) -> None:
    doc = doc or {}
    refs = doc.get("references", [])
    with_arxiv = [r["ref_arxiv_id"] for r in refs if r["ref_arxiv_id"]]
    quality = [r["metadata_status"] for r in refs]
    log.write(time=now_iso(), arxiv_id=arxiv_id, status=status,
              s2_paper_id=doc.get("s2_paper_id") or "",
              s2_reference_count=doc.get("s2_reference_count"),
              n_references=len(refs), n_with_arxiv_id=len(with_arxiv),
              n_unique_arxiv_id=len(set(with_arxiv)), n_complete=quality.count(COMPLETE),
              n_incomplete=quality.count(INCOMPLETE), n_unresolved=quality.count(UNRESOLVED),
              pages=doc.get("pages", 0), attempts=attempts, seconds=round(seconds, 3),
              error=error)


def run(papers: list[dict], layout: DataLayout, *, api_key: str | None = None,
        retry: tuple[str, ...] = (), resolve_dates: bool = True,
        client: SemanticScholar | None = None, dates: ArxivDates | None = None) -> dict:
    """Crawl the references of `papers` (manifest records with "arxiv_id").

    Papers with a final status are skipped unless the status is in `retry`;
    nothing finished is overwritten by a failed attempt. Returns the number
    of papers per status for this run.
    """
    layout.create()
    log = CsvLog(layout.logs / "references_log.csv", LOG_FIELDS)
    client = client or SemanticScholar(api_key or os.environ.get("S2_API_KEY", "").strip())
    dates = dates or ArxivDates(layout.work / "ref_arxiv_dates.jsonl", make_session(),
                                CsvLog(layout.logs / "arxiv_api_log.csv", API_LOG_FIELDS))
    worker = DateWorker(layout, dates) if resolve_dates else None

    todo = []
    for paper in papers:
        doc = load_doc(layout, paper["arxiv_id"])
        if doc is None or doc.get("status") not in FINAL_STATUSES or doc["status"] in retry:
            todo.append(paper["arxiv_id"])
        elif worker and doc.get("arxiv_dates_complete") is False:
            worker.inbox.put(doc)  # finished earlier, dates still missing
    print(f"[references] {len(todo)} papers to crawl, {len(papers) - len(todo)} already done "
          f"({'with' if client.has_key else 'without'} API key)")
    if worker:
        worker.start()

    counts: dict[str, int] = {}
    failed_in_a_row = 0
    try:
        for start in range(0, len(todo), LOOKUP_BATCH):
            chunk = todo[start:start + LOOKUP_BATCH]
            found, lookup_attempts, error = client.lookup(chunk)
            if error:
                for arxiv_id in chunk:
                    log_paper(log, arxiv_id, API_ERROR, None, lookup_attempts, 0, f"lookup: {error}")
                counts[API_ERROR] = counts.get(API_ERROR, 0) + len(chunk)
                failed_in_a_row += 1
            for arxiv_id in [] if error else chunk:
                started = time.perf_counter()
                doc, attempts, error = crawl_paper(client, arxiv_id, found[arxiv_id])
                status = doc["status"] if doc else API_ERROR
                failed_in_a_row = failed_in_a_row + 1 if doc is None else 0
                if doc:
                    complete = dates.fill(doc)  # dates already cached, if any
                    save(layout, doc)           # saved now, again once dates are filled
                    if worker and not complete:
                        worker.inbox.put(doc)
                log_paper(log, arxiv_id, status, doc, attempts, time.perf_counter() - started, error)
                counts[status] = counts.get(status, 0) + 1
                if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                    break
            if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                raise RuntimeError(f"Semantic Scholar failed {failed_in_a_row} times in a row: {error}")
    finally:
        if worker:  # even after an error, resolve the dates already queued
            print("[references] Semantic Scholar done; finishing arXiv date lookups")
            worker.finish()

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing new"
    print(f"[references] {summary}; {len(worker.pending) if worker else 0} papers still missing arXiv dates; "
          f"{getattr(client, 'throttled', 0)} HTTP 429 retried")
    return counts


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl references from Semantic Scholar")
    parser.add_argument("--data-dir", default="data", help="data root used by main.py (default: data)")
    parser.add_argument("--ids", nargs="+", help="crawl only these arXiv IDs (no manifest needed)")
    parser.add_argument("--limit", type=int, default=None, help="trial run: first N papers only")
    parser.add_argument("--retry", default="",
                        help="comma-separated final statuses to crawl again, e.g. not_found,unavailable")
    parser.add_argument("--no-arxiv-dates", action="store_true",
                        help="skip the arXiv calls for ref_submission_date (filled on a later run)")
    args = parser.parse_args(argv)
    args.retry = tuple(s.strip() for s in args.retry.split(",") if s.strip())
    if unknown := set(args.retry) - FINAL_STATUSES:
        parser.error(f"--retry accepts {', '.join(sorted(FINAL_STATUSES))}; got {', '.join(sorted(unknown))}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    layout = DataLayout(args.data_dir)
    if args.ids:
        papers = [{"arxiv_id": i} for i in args.ids]
    else:
        records = Manifest(layout.manifest).load().values()
        papers = sorted((r for r in records if r["status"] == SUCCESS),
                        key=lambda r: id_key(r["arxiv_id"]))
        if not papers:
            print(f"No found paper in {layout.manifest}; run discovery first or pass --ids.")
            return 1
    run(papers[:args.limit], layout, retry=args.retry, resolve_dates=not args.no_arxiv_dates)
    return 0


if __name__ == "__main__":
    sys.exit(main())