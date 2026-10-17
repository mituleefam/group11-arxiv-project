"""Member 3 - References crawling (Semantic Scholar Graph API).

For every paper found by discovery, writes two files in corpus/<yymm-id>/:

    references_all.json  every reference Semantic Scholar returns (working data)
        {"arxiv_id": "2605.12338", "status": "success",
         "s2_paper_id": "...", "s2_reference_count": 42, "pages": 1,
         "crawled_at": "...", "arxiv_dates_complete": true,
         "references": [{"ref_arxiv_id": "1706.03762" | "hep-th/9901001" | null,
                         "ref_title": "...", "ref_authors": ["..."],
                         "ref_submission_date": "2017-06-12T17:57:34Z" | null,
                         "ref_s2_id": "..." | null,
                         "ref_publication_date": "2017-06-12" | null, "ref_year": 2017,
                         "ref_venue": "..." | null, "ref_external_ids": {...},
                         "metadata_status": "complete" | "incomplete" | "unresolved"}]}

    references.json      only the references with an arXiv ID, in the format of
                         the Milestone 1 statement: a dictionary whose keys are
                         the cited arXiv IDs in "yymm-id" form (old-style IDs
                         such as "hep-th/9901001" kept unchanged):
        {"1706-03762": {"title": "...", "authors": ["..."],
                        "submission_date": "2017-06-12T17:57:34Z",
                        "semantic_scholar_id": "..."}}
                         table_rows() gives the same data as rows of the
                         `paper_references` table of the final dataset (M3).

Paper status (references_all.json and logs/references_log.csv):
    success      references were returned
    empty        Semantic Scholar knows the paper but lists no reference
    unavailable  it reports references (referenceCount > 0) but returns none,
                 e.g. when the publisher does not allow sharing them
    not_found    the paper is not indexed by Semantic Scholar
    api_error    the API kept failing; nothing is written, the next run retries
An empty answer is therefore never counted as a successful crawl.

Reference metadata_status:
    unresolved   Semantic Scholar could not link the citation to a paper
                 (no paperId; usually only a title is known)
    incomplete   linked to a paper, but the title or the authors are missing
    complete     paperId, title and authors are all present
This is independent of the arXiv ID: a complete reference may be a journal
article without one.

Dates. `ref_submission_date` is the arXiv submission date (v1) of the cited
paper, asked from the arXiv API with the IDs found in externalIds.ArXiv. It is
never taken from Semantic Scholar's publicationDate, which is usually the
journal or conference date; that one is kept separately as
`ref_publication_date`. arXiv answers are cached in work/ref_arxiv_dates.json
and every arXiv call goes through the shared rate_limiter.ARXIV.

API calls. One POST /paper/batch call looks up 500 papers at once (does it
exist, how many references); then /paper/{id}/references is read page by page
(1000 references per page) until no `next` page is left. Requests are spaced
1 s apart with an API key and 3 s apart without one (100 requests per
5 minutes), and 429/5xx/network errors are retried with exponential backoff.

API key: never put it in the repository. Set it in the environment:
    export S2_API_KEY=...                                  (shell)
    os.environ["S2_API_KEY"] = userdata.get("S2_API_KEY")  (Colab secret)

Usage. main.py calls run(papers, layout). The module also runs on its own:
    python src/references.py --data-dir data                 # every found paper
    python src/references.py --data-dir data --limit 20      # trial run
    python src/references.py --data-dir data --ids 2605.12338 2605.12345
    python src/references.py --data-dir data --retry not_found,unavailable
Re-running is safe: finished papers are skipped and nothing finished is
overwritten by a failed attempt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

from common import (API_ERROR, NOT_FOUND, SUCCESS, CsvLog, DataLayout, Manifest,
                    atomic_write_json, folder_name, now_iso)
from discovery import API_LOG_FIELDS, id_key, query_batch
from rate_limiter import ARXIV, RETRY_STATUS, RateLimiter, make_session

S2_API = "https://api.semanticscholar.org/graph/v1"
# Fields of each cited paper. They are requested flat: nested forms such as
# authors.name are rejected by the references endpoint.
REFERENCE_FIELDS = "paperId,title,authors,externalIds,publicationDate,year,venue"
LOOKUP_FIELDS = "paperId,referenceCount"
PAGE_SIZE = 1000     # maximum `limit` of /paper/{id}/references
MAX_PAGES = 10       # Semantic Scholar stops paging at offset 10 000
LOOKUP_BATCH = 500   # maximum number of IDs per /paper/batch call
ARXIV_BATCH = 100    # IDs per arXiv API call when resolving submission dates

API_KEY_ENV = ("S2_API_KEY", "SEMANTIC_SCHOLAR_API_KEY")
INTERVAL_WITH_KEY = 1.05     # 1 request per second
INTERVAL_WITHOUT_KEY = 3.1   # 100 requests per 5 minutes

# Paper statuses on top of common.SUCCESS / NOT_FOUND / API_ERROR.
EMPTY = "empty"
UNAVAILABLE = "unavailable"
FINAL_STATUSES = {SUCCESS, EMPTY, UNAVAILABLE, NOT_FOUND}

# Reference metadata statuses.
COMPLETE, INCOMPLETE, UNRESOLVED = "complete", "incomplete", "unresolved"

# Stop when this many papers in a row fail: the API is probably unreachable.
MAX_FAILED_IN_A_ROW = 5

# Columns of logs/references_log.csv: one row per paper and attempt.
LOG_FIELDS = ["time", "arxiv_id", "status", "s2_paper_id", "s2_reference_count",
              "n_references", "n_with_arxiv_id", "n_unique_arxiv_id", "n_complete",
              "n_incomplete", "n_unresolved", "pages", "attempts", "seconds", "error"]


# ---------------------------------------------------------------------------
# arXiv identifiers of cited papers
# ---------------------------------------------------------------------------

NEW_ID = re.compile(r"(?P<yy>\d{2})(?P<mm>\d{2})\.(?P<number>\d{4,5})")
# Before April 2007: archive[.SUBJECT-CLASS]/YYMMNNN, e.g. hep-th/9901001, math.GT/0309136
OLD_ID = re.compile(r"(?P<archive>[a-z][a-z\-]*)(?P<subject>\.[A-Za-z\-]+)?/(?P<yy>\d{2})(?P<mm>\d{2})\d{3}")


def normalize_arxiv_id(value) -> str | None:
    """Clean an externalIds.ArXiv value: drop an 'arXiv:' prefix and a version
    suffix, and check it is a real modern or old-style identifier. Old-style
    IDs are kept as given (the statement asks to retain them). Returns None
    for anything that is not a valid arXiv ID."""
    if not value:
        return None
    text = re.sub(r"^arxiv:", "", str(value).strip(), flags=re.IGNORECASE)
    text = re.sub(r"v\d+$", "", text)

    match = NEW_ID.fullmatch(text)
    if match:
        yymm, width = int(match["yy"] + match["mm"]), len(match["number"])
        valid_month = 1 <= int(match["mm"]) <= 12
        # 4-digit numbers from 0704 to 1412, 5-digit numbers from 1501.
        valid_width = (704 <= yymm <= 1412 and width == 4) or (yymm >= 1501 and width == 5)
        return text if valid_month and valid_width else None

    match = OLD_ID.fullmatch(text)
    if match and 1 <= int(match["mm"]) <= 12:
        return text
    return None


def arxiv_lookup_key(arxiv_id: str) -> str:
    """ID as the arXiv API reports it: the subject class of old-style IDs is
    not part of the identifier ('math.GT/0309136' -> 'math/0309136')."""
    return re.sub(r"^([a-z\-]+)\.[A-Za-z\-]+/", r"\1/", arxiv_id)


# ---------------------------------------------------------------------------
# Semantic Scholar client
# ---------------------------------------------------------------------------

class S2AuthError(RuntimeError):
    """HTTP 401/403: the API key is wrong or missing rights. Retrying is useless."""


def find_api_key() -> str | None:
    for name in API_KEY_ENV:
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    return None


class SemanticScholar:
    """Small client for the Graph API with its own rate limiter and retries."""

    def __init__(self, api_key: str | None = None, *, session=None,
                 limiter: RateLimiter | None = None, timeout: float = 60,
                 max_attempts: int = 6, first_backoff: float = 5.0):
        self.session = session if session is not None else make_session()
        self.has_key = bool(api_key)
        if api_key:
            self.session.headers["x-api-key"] = api_key
        self.limiter = limiter or RateLimiter(INTERVAL_WITH_KEY if api_key else INTERVAL_WITHOUT_KEY)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.first_backoff = first_backoff

    def call(self, method: str, path: str, *, params=None, payload=None):
        """One API call with retries for network errors, invalid JSON and HTTP
        429/5xx (waiting 5, 10, 20, 40, 80 s, or longer if the server sends
        Retry-After). Returns (data, attempts, error, http_status); `error` is
        "" on success."""
        url = f"{S2_API}/{path}"
        backoff = self.first_backoff
        error, status = "", None
        for attempt in range(1, self.max_attempts + 1):
            response = None
            with self.limiter:
                try:
                    response = self.session.request(method, url, params=params, json=payload,
                                                    timeout=self.timeout)
                    status = response.status_code
                    if status == 200:
                        return response.json(), attempt, "", status
                    error = f"HTTP {status}"
                except requests.RequestException as exc:  # also invalid JSON in a 200
                    error = f"{type(exc).__name__}: {exc}"
                    if response is None:
                        status = None
            if status in (401, 403):
                raise S2AuthError(f"Semantic Scholar answered {error}; check the S2_API_KEY value")
            if status is not None and status != 200 and status not in RETRY_STATUS:
                return None, attempt, error, status  # e.g. 404: retrying will not help
            if attempt < self.max_attempts:
                time.sleep(max(backoff, _retry_after(response)))
                backoff = min(backoff * 2, 120)
        return None, self.max_attempts, error, status

    def lookup(self, arxiv_ids: list[str]):
        """Look up up to 500 arXiv IDs in one call. Returns
        ({arxiv_id: {"paperId", "referenceCount"} or None}, attempts, error).
        The batch endpoint answers in the order of the request, with null for
        papers it does not know."""
        data, attempts, error, _ = self.call(
            "POST", "paper/batch", params={"fields": LOOKUP_FIELDS},
            payload={"ids": [f"arXiv:{i}" for i in arxiv_ids]})
        if error:
            return None, attempts, error
        if not isinstance(data, list) or len(data) != len(arxiv_ids):
            return None, attempts, "unexpected /paper/batch response"
        return dict(zip(arxiv_ids, data)), attempts, ""

    def references(self, paper_id: str):
        """Every reference of a paper, following the `next` offset until the
        last page. A failure on any page fails the whole paper, so a partial
        list is never saved. Returns (items, pages, attempts, error, status)."""
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


def _retry_after(response) -> float:
    if response is None:
        return 0.0
    try:
        return float(response.headers.get("Retry-After", 0))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _clean(text) -> str | None:
    text = " ".join(str(text).split()) if text else ""
    return text or None


def normalize_reference(item: dict | None) -> dict:
    """Turn one entry of /references ({"citedPaper": {...}}) into our format.
    Missing values stay null: nothing is guessed."""
    cited = (item or {}).get("citedPaper") or {}
    external = cited.get("externalIds") or {}
    title = _clean(cited.get("title"))
    authors = [name for name in (_clean(a.get("name")) for a in cited.get("authors") or [] if a)
               if name]
    ref = {
        "ref_arxiv_id": normalize_arxiv_id(external.get("ArXiv")),
        "ref_title": title,
        "ref_authors": authors,
        "ref_submission_date": None,  # filled from the arXiv API, see ArxivDates
        "ref_s2_id": cited.get("paperId"),
        "ref_publication_date": cited.get("publicationDate"),  # journal/venue date, not arXiv
        "ref_year": cited.get("year"),
        "ref_venue": _clean(cited.get("venue")),
        "ref_external_ids": external,
    }
    if not ref["ref_s2_id"]:
        ref["metadata_status"] = UNRESOLVED
    elif not (title and authors):
        ref["metadata_status"] = INCOMPLETE
    else:
        ref["metadata_status"] = COMPLETE
    return ref


def reference_key(ref_arxiv_id: str) -> str:
    """Key of references.json: '1706.03762' -> '1706-03762'; old IDs unchanged."""
    return ref_arxiv_id if "/" in ref_arxiv_id else ref_arxiv_id.replace(".", "-")


def references_dict(doc: dict) -> dict:
    """Content of references.json: {yymm-id: metadata} for every cited paper
    with an arXiv ID (the first entry wins when S2 lists a paper twice)."""
    out: dict[str, dict] = {}
    for ref in doc["references"]:
        if ref["ref_arxiv_id"]:
            out.setdefault(reference_key(ref["ref_arxiv_id"]), {
                "title": ref["ref_title"],
                "authors": ref["ref_authors"],
                "submission_date": ref["ref_submission_date"],
                "semantic_scholar_id": ref["ref_s2_id"],
            })
    return out


def table_rows(doc: dict) -> list[dict]:
    """Rows of the `paper_references` table of the final dataset (Milestone 3):
    references with an arXiv ID, one row per cited arXiv paper."""
    rows, seen = [], set()
    for ref in doc["references"]:
        ref_id = ref["ref_arxiv_id"]
        if ref_id and ref_id not in seen:
            seen.add(ref_id)
            rows.append({
                "arxiv_id": doc["arxiv_id"],
                "ref_arxiv_id": ref_id,
                "ref_title": ref["ref_title"],
                "ref_authors": ref["ref_authors"],
                "ref_submission_date": ref["ref_submission_date"],
                "ref_s2_id": ref["ref_s2_id"],
            })
    return rows


# ---------------------------------------------------------------------------
# arXiv submission dates of cited papers
# ---------------------------------------------------------------------------

class ArxivDates:
    """Cache of v1 submission dates from the arXiv API (work/ref_arxiv_dates.json).

    A cached value of null means arXiv answered and has no paper under that ID.
    IDs whose lookup failed are not cached, so they are asked again later.
    """

    def __init__(self, path: Path, session, log: CsvLog, *,
                 limiter: RateLimiter = ARXIV, query=None):
        self.path = Path(path)
        self.log = log
        self.cache: dict[str, str | None] = {}
        if self.path.exists():
            try:
                self.cache = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.cache = {}
        if query is None:
            def query(ids):
                return query_batch(session, ids, limiter)
        self.query = query

    def known(self, arxiv_id: str) -> bool:
        return arxiv_lookup_key(arxiv_id) in self.cache

    def date(self, arxiv_id: str) -> str | None:
        return self.cache.get(arxiv_lookup_key(arxiv_id))

    def resolve(self, arxiv_ids) -> None:
        """Ask arXiv about every ID not cached yet, 100 per call."""
        todo = sorted({arxiv_lookup_key(i) for i in arxiv_ids} - set(self.cache))
        for start in range(0, len(todo), ARXIV_BATCH):
            self._resolve_batch(todo[start:start + ARXIV_BATCH])
        if todo:
            atomic_write_json(self.path, self.cache)

    def _resolve_batch(self, batch: list[str]) -> None:
        started = time.perf_counter()
        entries, attempts, error = self.query(batch)
        self.log.write(time=now_iso(), stage="ref_dates", first_id=batch[0], last_id=batch[-1],
                       requested=len(batch), returned=len(entries), attempts=attempts,
                       seconds=round(time.perf_counter() - started, 3), error=error)
        if error.startswith("ArxivApiError"):
            # arXiv rejects the whole call if one ID is malformed: split to isolate it.
            if len(batch) == 1:
                self.cache[batch[0]] = None
            else:
                half = len(batch) // 2
                self._resolve_batch(batch[:half])
                self._resolve_batch(batch[half:])
            return
        if error:
            return  # network / HTTP error: leave uncached, retried later
        found = {entry["arxiv_id"]: entry["published"] or None for entry in entries}
        for arxiv_id in batch:
            self.cache[arxiv_id] = found.get(arxiv_id)

    def fill(self, doc: dict) -> bool:
        """Copy cached dates into a references_all document. Returns True when
        every arXiv reference has an answer from arXiv."""
        complete = True
        for ref in doc["references"]:
            if ref["ref_arxiv_id"]:
                if self.known(ref["ref_arxiv_id"]):
                    ref["ref_submission_date"] = self.date(ref["ref_arxiv_id"])
                else:
                    complete = False
        doc["arxiv_dates_complete"] = complete
        return complete


# ---------------------------------------------------------------------------
# Crawling
# ---------------------------------------------------------------------------

def crawl_paper(client: SemanticScholar, arxiv_id: str, s2_paper: dict | None):
    """Fetch and normalize the references of one paper.

    Returns (doc, attempts, error): `doc` is the references_all.json content,
    or None when the API failed (status api_error, nothing to save)."""
    doc = {"arxiv_id": arxiv_id, "status": None, "s2_paper_id": None,
           "s2_reference_count": None, "pages": 0, "crawled_at": now_iso(),
           "arxiv_dates_complete": True, "references": []}
    if s2_paper is None:
        doc["status"] = NOT_FOUND
        return doc, 0, ""

    doc["s2_paper_id"] = s2_paper.get("paperId")
    doc["s2_reference_count"] = count = s2_paper.get("referenceCount")
    if count == 0 or not doc["s2_paper_id"]:
        doc["status"] = EMPTY if doc["s2_paper_id"] else NOT_FOUND
        return doc, 0, ""

    items, pages, attempts, error, status = client.references(doc["s2_paper_id"])
    if error:
        if status == 404:
            doc["status"] = NOT_FOUND
            return doc, attempts, error
        return None, attempts, error

    doc["pages"] = pages
    doc["references"] = [normalize_reference(item) for item in items]
    if doc["references"]:
        doc["status"] = SUCCESS
    else:
        doc["status"] = UNAVAILABLE if (count or 0) > 0 else EMPTY
    return doc, attempts, ""


def save(layout: DataLayout, doc: dict) -> None:
    """references.json first, references_all.json last: references_all.json
    is the marker that the paper is finished."""
    folder = layout.paper_dir(doc["arxiv_id"])
    atomic_write_json(folder / "references.json", references_dict(doc))
    atomic_write_json(folder / "references_all.json", doc)


def load_doc(layout: DataLayout, arxiv_id: str) -> dict | None:
    path = layout.paper_dir(arxiv_id) / "references_all.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None  # damaged file: crawl again


def log_paper(log: CsvLog, arxiv_id: str, status: str, doc: dict | None,
              attempts: int, seconds: float, error: str) -> None:
    refs = doc["references"] if doc else []
    with_arxiv = [r["ref_arxiv_id"] for r in refs if r["ref_arxiv_id"]]
    log.write(time=now_iso(), arxiv_id=arxiv_id, status=status,
              s2_paper_id=(doc or {}).get("s2_paper_id") or "",
              s2_reference_count=(doc or {}).get("s2_reference_count"),
              n_references=len(refs), n_with_arxiv_id=len(with_arxiv),
              n_unique_arxiv_id=len(set(with_arxiv)),
              n_complete=sum(r["metadata_status"] == COMPLETE for r in refs),
              n_incomplete=sum(r["metadata_status"] == INCOMPLETE for r in refs),
              n_unresolved=sum(r["metadata_status"] == UNRESOLVED for r in refs),
              pages=(doc or {}).get("pages", 0), attempts=attempts,
              seconds=round(seconds, 3), error=error)


def run(papers: list[dict], layout: DataLayout, *, api_key: str | None = None,
        retry: tuple[str, ...] = (), resolve_dates: bool = True,
        client: SemanticScholar | None = None, dates: ArxivDates | None = None) -> dict:
    """Crawl the references of `papers` (manifest records with "arxiv_id").

    Papers whose references_all.json already has a final status are skipped,
    unless that status is listed in `retry`. Returns the number of papers per
    status for this run.
    """
    layout.create()
    log = CsvLog(layout.logs / "references_log.csv", LOG_FIELDS)
    if client is None:
        client = SemanticScholar(api_key or find_api_key())
    if dates is None:
        dates = ArxivDates(layout.work / "ref_arxiv_dates.json", make_session(),
                           CsvLog(layout.logs / "arxiv_api_log.csv", API_LOG_FIELDS))

    todo, pending = [], {}  # pending: finished papers still missing arXiv dates
    for paper in papers:
        doc = load_doc(layout, paper["arxiv_id"])
        if doc is None or doc.get("status") not in FINAL_STATUSES or doc["status"] in retry:
            todo.append(paper["arxiv_id"])
        elif not doc.get("arxiv_dates_complete", True):
            pending[doc["arxiv_id"]] = doc
    print(f"[references] {len(todo)} papers to crawl, {len(papers) - len(todo)} already done "
          f"({'with' if client.has_key else 'without'} API key)")

    def missing_dates() -> set[str]:
        return {r["ref_arxiv_id"] for doc in pending.values() for r in doc["references"]
                if r["ref_arxiv_id"] and not dates.known(r["ref_arxiv_id"])}

    flush_at = ARXIV_BATCH  # resolve dates once this many cited IDs are waiting

    def flush_dates() -> None:
        """Resolve dates for pending papers and rewrite their files."""
        nonlocal flush_at
        if not resolve_dates or not pending:
            return
        dates.resolve(missing_dates())
        for arxiv_id, doc in list(pending.items()):
            if dates.fill(doc):
                del pending[arxiv_id]
            save(layout, doc)
        # If arXiv failed, wait for a full new batch before asking again.
        flush_at = len(missing_dates()) + ARXIV_BATCH

    counts: dict[str, int] = {}
    failed_in_a_row = 0
    for start in range(0, len(todo), LOOKUP_BATCH):
        chunk = todo[start:start + LOOKUP_BATCH]
        found, lookup_attempts, error = client.lookup(chunk)
        if error:
            for arxiv_id in chunk:
                log_paper(log, arxiv_id, API_ERROR, None, lookup_attempts, 0, f"lookup: {error}")
            counts[API_ERROR] = counts.get(API_ERROR, 0) + len(chunk)
            failed_in_a_row += 1
            if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                raise RuntimeError(f"Semantic Scholar failed repeatedly: {error}")
            continue

        for arxiv_id in chunk:
            started = time.perf_counter()
            doc, attempts, error = crawl_paper(client, arxiv_id, found[arxiv_id])
            if doc is None:
                status = API_ERROR
                failed_in_a_row += 1
            else:
                status = doc["status"]
                failed_in_a_row = 0
                if not dates.fill(doc):  # some cited IDs are not in the cache yet
                    pending[arxiv_id] = doc
                save(layout, doc)
            log_paper(log, arxiv_id, status, doc, attempts, time.perf_counter() - started, error)
            counts[status] = counts.get(status, 0) + 1
            if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                flush_dates()
                raise RuntimeError(f"Semantic Scholar failed {failed_in_a_row} papers in a row: {error}")
            if resolve_dates and len(missing_dates()) >= flush_at:
                flush_dates()

    flush_dates()
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing new"
    print(f"[references] {summary}; {len(pending)} papers still missing arXiv dates")
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
    unknown = set(args.retry) - FINAL_STATUSES
    if unknown:
        parser.error(f"--retry accepts {', '.join(sorted(FINAL_STATUSES))}; got {', '.join(sorted(unknown))}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    layout = DataLayout(args.data_dir)
    if args.ids:
        papers = [{"arxiv_id": i, "folder": folder_name(i)} for i in args.ids]
    else:
        records = Manifest(layout.manifest).load().values()
        papers = sorted((r for r in records if r["status"] == SUCCESS),
                        key=lambda r: id_key(r["arxiv_id"]))
        if not papers:
            print(f"No found paper in {layout.manifest}; run discovery first or pass --ids.")
            return 1
    if args.limit is not None:
        papers = papers[:args.limit]
    run(papers, layout, retry=args.retry, resolve_dates=not args.no_arxiv_dates)
    return 0


if __name__ == "__main__":
    sys.exit(main())