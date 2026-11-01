"""Write the list of every arXiv ID of an assigned range, for validate.py --assigned-ids.

validate.py proves that no ID of the range is missing by comparing the
manifest with this list. Within one month the list is simply every number
from the first ID to the last. A range over several months also needs the
last number of every month but the end month, which the assignment does not
give: discovery.py finds it by asking the arXiv API until a whole batch comes
back empty, so a list built from the manifest would only check discovery
against itself. Here the last number comes from arXiv's metadata on Kaggle
(the snapshot ref_dates_kaggle.py reads), a source independent of the crawl:
if discovery stopped a month too early, validate.py reports the IDs it left
out as assigned_id_missing.

    python src/assigned_ids.py --start 2605.12338 --end 2605.17337 --out ds_id.txt
    python src/assigned_ids.py --start 2604.25001 --end 2605.04000 --out ds_id.txt --download
    python src/assigned_ids.py --start 2604.25001 --end 2605.04000 --out ds_id.txt --snapshot /content/arxiv.zip

Every number up to a month's last one is listed, gaps included: discovery
records a gap as not_found, so it is in the manifest too. The snapshot is
updated weekly, so a month is only taken as over when the snapshot already
holds a later month; otherwise no list is written (exit code 1).

Exit codes: 0 list written; 1 the snapshot cannot tell where a month ends.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from discovery import RangeBound, make_id, months_between
from ref_dates_kaggle import LEADING_ID, download_snapshot, open_snapshot

MODERN_ID = re.compile(rb"(\d{4})\.(\d{4,5})")


def month_ends(path: Path, months: set[str]) -> tuple[dict[str, int], str]:
    """Highest ID number of each of `months` in the snapshot, and the newest
    month the snapshot holds (to tell an old snapshot from a short month)."""
    ends: dict[str, int] = {}
    newest = ""
    with open_snapshot(path) as stream:
        for line in stream:
            match = LEADING_ID.match(line)
            found = MODERN_ID.fullmatch(match[1]) if match else None
            if not found:
                continue  # old-style ID such as hep-th/9901001, or a damaged line
            yymm, number = found[1].decode(), int(found[2])
            newest = max(newest, yymm)
            if yymm in months and number > ends.get(yymm, 0):
                ends[yymm] = number
    return ends, newest


def assigned_ids(start: RangeBound, end: RangeBound, ends: dict[str, int]) -> list[str]:
    ids = []
    for yymm in months_between(start.yymm, end.yymm):
        first = start.number if yymm == start.yymm else 1
        last = end.number if yymm == end.yymm else ends[yymm]
        ids += [make_id(yymm, n) for n in range(first, last + 1)]
    return ids


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List every arXiv ID of an assigned range")
    parser.add_argument("--start", required=True, help="first ID, e.g. 2605.12338 or 2026-05:12338")
    parser.add_argument("--end", required=True, help="last ID")
    parser.add_argument("--out", type=Path, required=True, help="the list to write, one ID per line")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--snapshot", type=Path, help="Kaggle metadata already on disk (.json, .json.gz or .zip)")
    source.add_argument("--download", action="store_true",
                        help="get the Kaggle metadata with kagglehub (kept in its cache)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    start, end = RangeBound.parse(args.start), RangeBound.parse(args.end)
    if (int(end.yymm), end.number) < (int(start.yymm), start.number):
        raise SystemExit(f"--end {args.end} is before --start {args.start}")
    open_months = set(months_between(start.yymm, end.yymm)) - {end.yymm}
    ends: dict[str, int] = {}
    if open_months:
        if not (args.snapshot or args.download):
            raise SystemExit("The range spans several months: give --snapshot or --download "
                             "to learn where each month ends")
        started = time.perf_counter()
        path = args.snapshot or download_snapshot()
        ends, newest = month_ends(path, open_months)
        print(f"[assigned_ids] read {path} in {time.perf_counter() - started:.0f} s; "
              f"newest month in it: {newest or '?'}", flush=True)
        for yymm in sorted(open_months):
            # A month is only known to be over once the snapshot holds a later month.
            if yymm not in ends or newest <= yymm:
                print(f"[assigned_ids] the snapshot does not show where month {yymm} ends "
                      f"(newest month in it: {newest or '?'}): no list written", flush=True)
                return 1
            print(f"[assigned_ids] month {yymm} ends at {make_id(yymm, ends[yymm])}", flush=True)
        if ends.get(start.yymm, start.number) < start.number:
            print(f"[assigned_ids] warning: month {start.yymm} ends at "
                  f"{make_id(start.yymm, ends[start.yymm])}, before --start", flush=True)
    ids = assigned_ids(start, end, ends)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(ids) + "\n" if ids else "", encoding="utf-8")
    print(f"[assigned_ids] {len(ids)} IDs from {args.start} to {args.end} -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
