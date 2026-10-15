"""Member 1 - Entry discovery.

Turns the assigned range (start_month, start_id, end_month, end_id) into arXiv
IDs, asks the arXiv API which of them exist and how many versions each one
has, and records the answer in the paper manifest (work/paper_manifest.jsonl),
one line per ID:

    {"arxiv_id": "2605.12428", "folder": "2605-12428", "status": "success",
     "latest_version": 2, "versions": ["v1", "v2"], "latest": {...}, ...}

`latest` keeps the API entry of the newest version, so metadata.py does not
need to ask for that version again.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from common import API_ERROR, NOT_FOUND, SUCCESS, CsvLog, Manifest, folder_name, now_iso
from rate_limiter import ARXIV, RateLimiter, get_with_retry

API_URL = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
ENTRY_ID = re.compile(r"abs/(?P<base>.+?)v(?P<version>\d+)$")

# Columns of logs/arxiv_api_log.csv: one row per API call (also used by metadata.py).
API_LOG_FIELDS = ["time", "stage", "first_id", "last_id", "requested",
                  "returned", "attempts", "seconds", "error"]

# Stop when this many batches in a row fail: arXiv is probably unreachable.
MAX_FAILED_BATCHES = 3


# ---------------------------------------------------------------------------
# arXiv IDs
# ---------------------------------------------------------------------------

def month_to_yymm(month: str) -> str:
    """'2026-05' (registration sheet) or '2605' -> '2605'."""
    month = month.strip()
    if "-" in month:
        year, mon = month.split("-")
        yymm = f"{int(year) % 100:02d}{int(mon):02d}"
    else:
        yymm = month
    if not re.fullmatch(r"\d{4}", yymm) or not 1 <= int(yymm[2:]) <= 12:
        raise ValueError(f"invalid month: {month!r}")
    return yymm


def number_width(yymm: str) -> int:
    """New-style IDs use 4 digits up to 1412 and 5 digits from 1501."""
    return 5 if int(yymm) >= 1501 else 4


def make_id(yymm: str, number: int) -> str:
    """('2605', 42) -> '2605.00042'."""
    return f"{yymm}.{number:0{number_width(yymm)}d}"


def id_key(arxiv_id: str) -> tuple[int, int]:
    """Sort key: '2605.00042' -> (2605, 42)."""
    yymm, number = arxiv_id.split(".")
    return int(yymm), int(number)


@dataclass(frozen=True)
class RangeBound:
    """One end of the assigned range, e.g. month '2605' and number 12338."""

    yymm: str
    number: int

    @classmethod
    def parse(cls, text: str) -> "RangeBound":
        """Accept '2026-05:12338' (sheet columns joined by ':') or '2605.12338'."""
        text = text.strip()
        if ":" in text:
            month, number = text.split(":", 1)
        elif "." in text:
            month, number = text.split(".", 1)
        else:
            raise ValueError(f"expected 2026-05:12338 or 2605.12338, got {text!r}")
        bound = cls(month_to_yymm(month), int(number))
        if bound.number < 1:
            raise ValueError(f"ID number must be positive: {text!r}")
        return bound

    def as_id(self) -> str:
        return make_id(self.yymm, self.number)


def months_between(first: str, last: str) -> list[str]:
    """All yymm months from `first` to `last`, inclusive."""
    if int(last) < int(first):
        raise ValueError(f"end month {last} is before start month {first}")
    year, month = int(first[:2]), int(first[2:])
    months = []
    while True:
        current = f"{year:02d}{month:02d}"
        months.append(current)
        if current == last:
            return months
        month += 1
        if month == 13:
            year, month = year + 1, 1


# ---------------------------------------------------------------------------
# arXiv API
# ---------------------------------------------------------------------------

class ArxivApiError(Exception):
    """The API answered with an error entry instead of papers."""


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """Turn an Atom response into a list of entries (one per paper version)."""
    root = ET.fromstring(xml_bytes)
    entries = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = entry.findtext(f"{ATOM}id", "").strip()
        if "/api/errors" in raw_id:
            raise ArxivApiError(entry.findtext(f"{ATOM}summary", "").strip())
        match = ENTRY_ID.search(raw_id)
        if match is None:
            continue
        journal_ref = entry.find(f"{ARXIV_NS}journal_ref")
        primary = entry.find(f"{ARXIV_NS}primary_category")
        entries.append({
            "arxiv_id": match["base"],
            "version": int(match["version"]),
            "title": " ".join(entry.findtext(f"{ATOM}title", "").split()),
            "authors": [" ".join(author.findtext(f"{ATOM}name", "").split())
                        for author in entry.findall(f"{ATOM}author")],
            "published": entry.findtext(f"{ATOM}published", "").strip(),  # date of v1
            "updated": entry.findtext(f"{ATOM}updated", "").strip(),      # date of this version
            "journal_ref": (" ".join(journal_ref.text.split())
                            if journal_ref is not None and journal_ref.text else None),
            "primary_category": primary.get("term") if primary is not None else None,
            "categories": [c.get("term") for c in entry.findall(f"{ATOM}category")],
        })
    return entries


def query_batch(session, ids: list[str], limiter: RateLimiter = ARXIV):
    """Ask the API about a batch of IDs (plain like '2605.12338' or versioned
    like '2605.12338v2') in one call.

    max_results must equal the batch size, because the API returns only 10
    entries by default. Entries come back in no particular order and unknown
    IDs are silently left out, so callers must match results by ID.

    Returns (entries, attempts, error); `error` is "" on success.
    """
    params = {"id_list": ",".join(ids), "max_results": len(ids)}
    response, attempts, error = get_with_retry(session, API_URL, params=params, limiter=limiter)
    if error:
        return [], attempts, error
    try:
        return parse_feed(response.content), attempts, ""
    except (ET.ParseError, ArxivApiError) as exc:
        return [], attempts, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _record(arxiv_id: str, status: str, **fields) -> dict:
    return {"arxiv_id": arxiv_id, "folder": folder_name(arxiv_id), "status": status,
            "checked_at": now_iso(), **fields}


def discover_range(start: RangeBound, end: RangeBound, session, manifest: Manifest,
                   log: CsvLog, *, limiter: RateLimiter = ARXIV, batch_size: int = 100,
                   limit: int | None = None, query=None) -> list[dict]:
    """Resolve every ID of the assigned range and record it in the manifest.

    Each record ends up as "success" (paper exists, with its version list),
    "not_found" (no paper with that ID) or "api_error" (the API kept failing;
    the ID is asked again on the next run).

    Months after the start month and before the end month are open-ended:
    nobody tells us the last number of such a month, so we keep asking in
    batches until a whole batch comes back empty. Missing IDs after the last
    paper of a batch are only marked "not_found" once a later paper shows they
    were a gap; if the month ends there, they were never part of the range.

    IDs already resolved in an earlier run are not asked again, so the function
    can simply be re-run after an interruption. `limit` keeps only the first
    `limit` IDs (trial runs). Returns the records of every ID in the range,
    in order.
    """
    if query is None:
        def query(ids):
            return query_batch(session, ids, limiter)

    done = manifest.load()
    in_range: list[str] = []
    budget = limit
    failed_in_a_row = 0

    def save(record: dict) -> None:
        manifest.append(record)
        done[record["arxiv_id"]] = record

    for yymm in months_between(start.yymm, end.yymm):
        number = start.number if yymm == start.yymm else 1
        last = end.number if yymm == end.yymm else None  # None: end of month unknown
        tail: list[str] = []  # missing IDs after the newest paper seen in this month

        while (last is None or number <= last) and (budget is None or budget > 0):
            stop = number + batch_size - 1
            if last is not None:
                stop = min(stop, last)
            if budget is not None:
                stop = min(stop, number + budget - 1)
                budget -= stop - number + 1
            ids = [make_id(yymm, k) for k in range(number, stop + 1)]
            number = stop + 1

            todo = [i for i in ids if done.get(i, {}).get("status") not in (SUCCESS, NOT_FOUND)]
            error = ""
            if todo:
                started = time.perf_counter()
                entries, attempts, error = query(todo)
                log.write(time=now_iso(), stage="discovery", first_id=todo[0], last_id=todo[-1],
                          requested=len(todo), returned=len(entries), attempts=attempts,
                          seconds=round(time.perf_counter() - started, 3), error=error)
                if error:
                    failed_in_a_row += 1
                    for arxiv_id in todo:
                        save(_record(arxiv_id, API_ERROR, error=error))
                    if failed_in_a_row >= MAX_FAILED_BATCHES:
                        raise RuntimeError(f"arXiv API failed {failed_in_a_row} batches in a row: {error}")
                else:
                    failed_in_a_row = 0
                    by_id = {entry["arxiv_id"]: entry for entry in entries}
                    for arxiv_id in todo:
                        entry = by_id.get(arxiv_id)
                        if entry is not None:
                            save(_record(arxiv_id, SUCCESS, latest_version=entry["version"],
                                         versions=[f"v{v}" for v in range(1, entry["version"] + 1)],
                                         latest=entry))

            found = [i for i in ids if done.get(i, {}).get("status") == SUCCESS]
            absent = [] if error else [i for i in todo if i not in found]

            if last is not None:  # the end of this month is known: every absent ID is a gap
                for arxiv_id in absent:
                    save(_record(arxiv_id, NOT_FOUND, latest_version=None, versions=[]))
                in_range.extend(ids)
            elif error:  # cannot tell yet; the batch is retried on the next run
                in_range.extend(ids)
            elif found:
                newest = max(id_key(i)[1] for i in found)
                for arxiv_id in tail:  # a later paper exists, so these were gaps
                    save(_record(arxiv_id, NOT_FOUND, latest_version=None, versions=[]))
                in_range.extend(tail)
                tail = [i for i in absent if id_key(i)[1] > newest]
                for arxiv_id in absent:
                    if id_key(arxiv_id)[1] < newest:
                        save(_record(arxiv_id, NOT_FOUND, latest_version=None, versions=[]))
                in_range.extend(i for i in ids if id_key(i)[1] <= newest)
            elif todo:  # asked and nothing there: this month has ended
                break
            else:  # every ID already known from an earlier run as a gap
                in_range.extend(ids)

    return [done[i] for i in in_range]


def known_records(manifest: Manifest, start: RangeBound, end: RangeBound) -> list[dict]:
    """Manifest records inside the range, for runs that skip discovery."""
    low, high = (int(start.yymm), start.number), (int(end.yymm), end.number)
    records = [r for r in manifest.load().values() if low <= id_key(r["arxiv_id"]) <= high]
    return sorted(records, key=lambda r: id_key(r["arxiv_id"]))
