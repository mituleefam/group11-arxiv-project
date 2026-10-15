"""Member 1 - Metadata collection.

Writes corpus/<yymm-id>/metadata.json for every paper found by discovery:

    {"arxiv_id": "2605.12428",
     "title": "...", "authors": ["..."],
     "submission_date": "2026-05-12T17:24:22Z",
     "revised_dates": ["2026-06-22T20:30:22Z"],
     "venue": null,
     "primary_category": "math.DS", "categories": ["math.DS"],
     "versions": [{"version": "v1", "date": "...", "title": "...", "authors": [...], "venue": null},
                  {"version": "v2", ...}]}

The top-level fields follow the `papers` table of the statement. `versions`
keeps the metadata of every version, as the statement's suggested data
organization asks. The newest version comes from the discovery call; older
versions are fetched with versioned IDs ("2605.12428v1"), 100 per API call.
A paper with a single version needs no extra call.
"""

from __future__ import annotations

import time

from common import SUCCESS, CsvLog, DataLayout, atomic_write_json, now_iso
from discovery import query_batch
from rate_limiter import ARXIV, RateLimiter


def build_metadata(arxiv_id: str, entries: dict[int, dict]) -> dict:
    """Combine the API entries of every version (version number -> entry)."""
    newest = entries[max(entries)]
    versions = [{
        "version": f"v{number}",
        "date": entries[number]["updated"],  # for a versioned query: date of that version
        "title": entries[number]["title"],
        "authors": entries[number]["authors"],
        "venue": entries[number]["journal_ref"],
    } for number in sorted(entries)]
    return {
        "arxiv_id": arxiv_id,
        "title": newest["title"],
        "authors": newest["authors"],
        "submission_date": newest["published"],  # every entry carries the date of v1
        "revised_dates": [v["date"] for v in versions[1:]],  # [] when never revised
        "venue": newest["journal_ref"],  # None when not published anywhere
        "primary_category": newest["primary_category"],
        "categories": newest["categories"],
        "versions": versions,
    }


def collect_metadata(papers: list[dict], layout: DataLayout, session, log: CsvLog, *,
                     limiter: RateLimiter = ARXIV, batch_size: int = 100,
                     query=None) -> tuple[int, list[str]]:
    """Write metadata.json for every found paper that does not have one yet.

    Returns (number of files written, IDs still incomplete because an API call
    failed). Incomplete papers get no file, so the next run retries them.
    """
    if query is None:
        def query(ids):
            return query_batch(session, ids, limiter)

    todo = [p for p in papers if p["status"] == SUCCESS
            and not (layout.paper_dir(p["arxiv_id"]) / "metadata.json").exists()]

    # The newest version is already known from discovery.
    entries = {p["arxiv_id"]: {p["latest_version"]: p["latest"]} for p in todo}
    wanted = [f"{p['arxiv_id']}v{v}" for p in todo for v in range(1, p["latest_version"])]

    for i in range(0, len(wanted), batch_size):
        batch = wanted[i:i + batch_size]
        started = time.perf_counter()
        received, attempts, error = query(batch)
        log.write(time=now_iso(), stage="metadata", first_id=batch[0], last_id=batch[-1],
                  requested=len(batch), returned=len(received), attempts=attempts,
                  seconds=round(time.perf_counter() - started, 3), error=error)
        for entry in received:  # match by ID: the order of results is not guaranteed
            if entry["arxiv_id"] in entries:
                entries[entry["arxiv_id"]][entry["version"]] = entry

    written, incomplete = 0, []
    for paper in todo:
        have = entries[paper["arxiv_id"]]
        if sorted(have) != list(range(1, paper["latest_version"] + 1)):
            incomplete.append(paper["arxiv_id"])
            continue
        path = layout.paper_dir(paper["arxiv_id"]) / "metadata.json"
        atomic_write_json(path, build_metadata(paper["arxiv_id"], have))
        written += 1
    return written, incomplete
