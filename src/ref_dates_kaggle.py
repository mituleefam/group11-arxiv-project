"""Fill ref_submission_date from arXiv's metadata on Kaggle instead of the arXiv API.

references.py (Member 3) asks the arXiv API for the v1 date of every cited
paper, 100 IDs per call: about 500 calls for a member's 5 000 papers, which
cite some 50 000 distinct arXiv IDs. These calls share the one request every
3 seconds allowed by arXiv with the source download, the slowest stage, and
every HTTP 429 holds both back. arXiv's bulk data page
(https://info.arxiv.org/help/bulk_data/index.html) lists Kaggle for its
metadata: arxiv-metadata-oai-snapshot.json in the Cornell-University/arxiv
dataset, one JSON line per arXiv paper, published by arXiv itself. The date
of the first version in it (versions[0].created) is the v1 date the API
returns as <published>.

Usage (Colab), after the crawl:

    python src/main.py --range 2026-05 12338 2026-05 17337 --data-dir data --no-arxiv-dates
    python src/ref_dates_kaggle.py --data-dir data --download
    python src/ref_dates_kaggle.py --data-dir data --snapshot /content/arxiv.zip
    python src/ref_dates_kaggle.py --data-dir data --cache /content/drive/MyDrive/g11/m2/work/ref_arxiv_dates.jsonl

--download gets the snapshot with kagglehub (preinstalled on Colab) into its
cache on the runtime's disk, outside the data folder; it is a public dataset,
and kagglehub asks for a Kaggle token (KAGGLE_API_TOKEN in Colab Secrets)
only if Kaggle requires one. --snapshot reads a copy already on disk: the
.json, a .json.gz or the .zip from Kaggle, read as a stream (never unpacked,
so the memory used stays small). --cache adds the dates other members already
have (their work/ref_arxiv_dates.jsonl, or the older .json), so that a paper
cited by several members is looked up once. Caches are read first; the
options can be combined.

What it does:
 1. lists the cited arXiv IDs of corpus/*/references_all.json that have no
    date in work/ref_arxiv_dates.jsonl yet;
 2. takes their dates from the caches, then from the snapshot, and appends
    them to work/ref_arxiv_dates.jsonl in the format references.py reads
    (plus a "source" field);
 3. writes references_all.json and references.json again for the papers that
    got dates, with references.save(), so the files are those references.py
    writes;
 4. leaves the IDs found nowhere (a paper newer than the snapshot, or an ID
    that Semantic Scholar got wrong) to the arXiv API: the next main.py run
    without --no-arxiv-dates asks arXiv about these IDs only.

No request is sent to arXiv. To check that both sources give the same date,
the snapshot is compared with up to --check dates the arXiv API already gave
(in the caches), and the result is printed and logged. Each run adds one row
to logs/ref_dates_kaggle_log.csv. Run it under benchmark.py so that its time,
download included, is counted in the benchmark:

    python src/benchmark.py --data-dir data -- python src/ref_dates_kaggle.py --data-dir data --download
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import zipfile
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from common import CsvLog, DataLayout, RunLock, now_iso
from references import ArxivDates, arxiv_lookup_key, load_doc, save

KAGGLE_DATASET = "Cornell-University/arxiv"
SNAPSHOT_FILE = "arxiv-metadata-oai-snapshot.json"
SOURCE = "kaggle " + SNAPSHOT_FILE
# Every line of the snapshot starts with the paper ID: {"id":"0704.0001","submitter":...
LEADING_ID = re.compile(rb'\{\s*"id"\s*:\s*"([^"]+)"')

LOG_FIELDS = ["time", "papers", "papers_incomplete", "ids_needed", "from_cache",
              "from_snapshot", "not_found", "papers_saved", "papers_complete",
              "checked", "check_mismatches", "snapshot", "download_seconds",
              "scan_seconds", "seconds"]


def v1_date(record: dict) -> str | None:
    """'Mon, 2 Apr 2007 19:18:42 GMT' (date of v1) -> '2007-04-02T19:18:42Z',
    the form of <published> in the arXiv API."""
    versions = record.get("versions") or []
    first = next((v for v in versions if v.get("version") == "v1"), versions[0] if versions else {})
    try:
        created = parsedate_to_datetime(first["created"])
    except (KeyError, TypeError, ValueError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def open_snapshot(path: Path):
    """The snapshot as a stream of byte lines, whether plain, gzip or zip."""
    with open(path, "rb") as raw:
        magic = raw.read(4)
        raw.seek(0)
        if magic.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(raw) as archive:
                member = max(archive.infolist(), key=lambda info: info.file_size)
                with archive.open(member) as stream:
                    yield stream
        elif magic.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=raw) as stream:
                yield stream
        else:
            yield raw


def scan_snapshot(path: Path, wanted: set[str]) -> tuple[dict[str, str], int]:
    """Dates of the `wanted` IDs found in the snapshot, and the number of lines
    read. Only matching lines are parsed, so a scan takes about as long as
    reading the file."""
    found: dict[str, str] = {}
    lines = 0
    with open_snapshot(path) as stream:
        for line in stream:
            lines += 1
            match = LEADING_ID.match(line)
            if match:
                key = match[1].decode()
                if "\\" in key:  # escaped character, e.g. "hep-th\/9901001"
                    key = json.loads(f'"{key}"')
                if key not in wanted:
                    continue
            try:
                record = json.loads(line)
            except ValueError:
                continue  # blank or damaged line
            key = record.get("id") if isinstance(record, dict) else None
            if key in wanted and (date := v1_date(record)):
                found[key] = date
    return found, lines


def download_snapshot() -> Path:
    try:
        import kagglehub
    except ImportError:
        raise SystemExit("--download needs kagglehub (preinstalled on Colab): pip install kagglehub")
    try:
        path = Path(kagglehub.dataset_download(KAGGLE_DATASET, path=SNAPSHOT_FILE))
    except TypeError:  # older kagglehub without `path`: the dataset is this one file anyway
        path = Path(kagglehub.dataset_download(KAGGLE_DATASET))
    if path.is_dir():
        path = next(iter(sorted(path.rglob(SNAPSHOT_FILE + "*"))), path)
    return path


def read_cache(path: Path) -> tuple[dict[str, str | None], list[str]]:
    """Dates in a ref_arxiv_dates.jsonl (or the older .json) file, and the IDs
    whose date came from the arXiv API (lines without "source")."""
    dates: dict[str, str | None] = {}
    from_api: list[str] = []
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":  # older format: one JSON object, filled by the arXiv API only
        try:
            legacy = json.loads(text)
        except ValueError:
            return dates, from_api
        return (legacy, [i for i, d in legacy.items() if d]) if isinstance(legacy, dict) else ({}, [])
    for line in text.splitlines():
        try:
            item = json.loads(line)
            dates[item["id"]] = item["date"]
        except (ValueError, KeyError, TypeError):
            continue  # line cut by a crash
        if item["date"] and "source" not in item:
            from_api.append(item["id"])
    return dates, from_api


def no_api(ids):
    raise RuntimeError("ref_dates_kaggle.py never calls the arXiv API")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill ref_submission_date without the arXiv API")
    parser.add_argument("--data-dir", default="data", help="data root used by main.py (default: data)")
    parser.add_argument("--snapshot", type=Path, help=f"{SNAPSHOT_FILE} (.json, .json.gz or .zip)")
    parser.add_argument("--download", action="store_true",
                        help=f"download {SNAPSHOT_FILE} from Kaggle ({KAGGLE_DATASET}) with kagglehub")
    parser.add_argument("--cache", type=Path, nargs="+", default=[],
                        help="other members' work/ref_arxiv_dates.jsonl (or .json) files")
    parser.add_argument("--check", type=int, default=200,
                        help="compare the snapshot with up to N dates from the arXiv API (default 200)")
    parser.add_argument("--force", action="store_true",
                        help="take over work/crawler.lock (only if the crawler that holds it is dead)")
    args = parser.parse_args(argv)
    if not (args.snapshot or args.download or args.cache):
        parser.error("give --snapshot, --download and/or --cache")
    if args.snapshot and args.download:
        parser.error("--snapshot and --download are alternatives")
    for path in [args.snapshot, *args.cache]:
        if path and not path.is_file():
            parser.error(f"file not found: {path}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    started = time.perf_counter()
    layout = DataLayout(args.data_dir)
    layout.create()
    lock = RunLock(layout.work / "crawler.lock")  # the crawler writes the same files
    try:
        lock.acquire(force=args.force)
    except RuntimeError as exc:
        print(f"Not started: {exc}")
        return 2
    try:
        return fill_dates(args, layout, started)
    except KeyboardInterrupt:
        print("\nStopped: dates found so far are saved; run the same command again.")
        return 130
    finally:
        lock.release()


def fill_dates(args: argparse.Namespace, layout: DataLayout, started: float) -> int:
    cache_path = layout.work / "ref_arxiv_dates.jsonl"
    dates = ArxivDates(cache_path, session=None, log=None, query=no_api)

    # 1. Papers whose dates are not complete, and the cited IDs still unknown.
    missing: dict[str, set[str]] = {}  # citing paper -> cited IDs (lookup keys) without a date
    papers = 0
    for path in sorted(layout.corpus.glob("*/references_all.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue  # damaged: references.py crawls it again
        papers += 1
        unknown = {arxiv_lookup_key(r["ref_arxiv_id"]) for r in doc["references"]
                   if r["ref_arxiv_id"] and not dates.known(r["ref_arxiv_id"])}
        if unknown or doc.get("arxiv_dates_complete") is False:
            missing[doc["arxiv_id"]] = unknown
    needed = set().union(*missing.values()) if missing else set()
    print(f"[ref dates] {papers} papers with references, {len(missing)} without every date, "
          f"{len(needed)} cited IDs to look up")

    # 2. Dates from the other members' caches, then from the snapshot.
    answers: dict[str, tuple[str | None, str]] = {}  # ID -> (date, source)
    from_api: dict[str, str] = {}  # dates the arXiv API gave, for the check
    own, own_api = read_cache(cache_path) if cache_path.exists() else ({}, [])
    from_api.update((i, own[i]) for i in own_api)
    legacy = cache_path.with_suffix(".json")
    if legacy.exists():
        old, old_api = read_cache(legacy)
        from_api.update((i, old[i]) for i in old_api)
    for path in args.cache:
        other, other_api = read_cache(path)
        from_api.update((i, other[i]) for i in other_api)
        for key in needed - set(answers):
            if key in other:
                answers[key] = (other[key], f"cache {path}")
    from_cache = len(answers)
    print(f"[ref dates] {from_cache} found in {len(args.cache)} other cache(s)")

    snapshot, download_seconds, scan_seconds = args.snapshot, 0.0, 0.0
    checked, mismatches, from_snapshot = 0, [], 0
    left = needed - set(answers)
    if args.download and left:
        print(f"[ref dates] downloading {SNAPSHOT_FILE} from Kaggle ({KAGGLE_DATASET})")
        clock = time.perf_counter()
        snapshot = download_snapshot()
        download_seconds = time.perf_counter() - clock
        print(f"[ref dates] {snapshot} ({snapshot.stat().st_size / 2**30:.2f} GiB) "
              f"in {download_seconds:.0f} s")
    if snapshot and left:
        items = sorted(from_api.items())
        step = max(1, len(items) // args.check) if args.check > 0 else 0
        to_check = dict(items[::step][:args.check]) if step else {}
        clock = time.perf_counter()
        found, lines = scan_snapshot(snapshot, left | set(to_check))
        scan_seconds = time.perf_counter() - clock
        for key, api_date in to_check.items():
            if key in found:
                checked += 1
                if found[key] != api_date:
                    mismatches.append(f"{key}: API {api_date}, snapshot {found[key]}")
        print(f"[ref dates] snapshot: {lines} papers read in {scan_seconds:.0f} s, "
              f"{len(left & found.keys())} of the {len(left)} missing dates found")
        if checked:
            print(f"[ref dates] check: {checked} dates also given by the arXiv API, "
                  f"{len(mismatches)} different")
        else:
            print("[ref dates] check: no date from the arXiv API to compare with (pass a cache "
                  "filled by the API with --cache to have one)")
        for line in mismatches[:10]:
            print(f"    {line}")
        if mismatches:
            print("[ref dates] the snapshot disagrees with the arXiv API: its dates are NOT used")
        else:
            for key in left & found.keys():
                answers[key] = (found[key], SOURCE)
                from_snapshot += 1

    # 3. Save the dates in the cache file references.py reads, then the papers.
    if answers:
        with open(cache_path, "a", encoding="utf-8") as f:
            f.writelines(json.dumps({"id": key, "date": date, "source": source}) + "\n"
                         for key, (date, source) in sorted(answers.items()))
        dates.cache.update((key, date) for key, (date, _) in answers.items())
    saved = complete = 0
    for arxiv_id, unknown in missing.items():
        if unknown and not unknown & answers.keys():
            continue  # nothing new for this paper
        doc = load_doc(layout, arxiv_id)
        if doc is None:
            continue
        complete += dates.fill(doc)
        save(layout, doc)
        saved += 1
    not_found = len(needed) - len(answers)
    print(f"[ref dates] {saved} papers saved, {complete} now complete; {not_found} IDs found "
          f"nowhere, left to the arXiv API (main.py without --no-arxiv-dates)")

    CsvLog(layout.logs / "ref_dates_kaggle_log.csv", LOG_FIELDS).write(
        time=now_iso(), papers=papers, papers_incomplete=len(missing), ids_needed=len(needed),
        from_cache=from_cache, from_snapshot=from_snapshot, not_found=not_found,
        papers_saved=saved, papers_complete=complete, checked=checked,
        check_mismatches=len(mismatches), snapshot=str(snapshot or ""),
        download_seconds=round(download_seconds, 3), scan_seconds=round(scan_seconds, 3),
        seconds=round(time.perf_counter() - started, 3))
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
