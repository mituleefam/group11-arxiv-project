"""Group benchmark: one table for the crawls of every member.

The benchmark is asked on the data of the whole group, with the retry and
waiting time counted in the running time. Each member crawls their own range
on their own Colab runtime, so the numbers are spread over one data folder
per member. This tool reads those folders and writes one row per member plus
a row for the group. It sends no request and changes nothing in the folders.

Usage (each argument is a member's --data-dir, optionally named NAME=PATH;
the folder name is used otherwise):

    python src/group_benchmark.py m1=/content/drive/MyDrive/g11/m1 m2=/content/drive/MyDrive/g11/m2
    python src/group_benchmark.py data_m1 data_m2 data_m3 --out group_benchmark

Outputs in --out (default: group_benchmark):
    group_benchmark.csv   one row per member, then the "group" row
    group_benchmark.json  the same, with the definitions and warnings

Where the numbers come from (<data-dir>/logs unless noted):
    wall_seconds         sum of the benchmark.py invocations, read with
                         benchmark_runs.aggregate (Member 4): everything the
                         runs took, waits and retry passes included.
                         Invocations that died without a summary are listed
                         in the warnings, not counted.
    crawler_seconds      sum of the passes in pass_log.csv (main.py's own
                         clock, without the delay between passes): a
                         cross-check, and the only figure when a member did
                         not run the crawler under benchmark.py
    kaggle_dates_seconds ref_dates_kaggle.py runs (ref_dates_kaggle_log.csv),
                         already inside wall_seconds when run under benchmark.py
    arxiv_requests       HTTP requests sent to arXiv: arxiv_api_requests (the
                         attempts in arxiv_api_log.csv) plus
                         arxiv_download_requests (the attempts in download_log.csv)
    arxiv_floor_seconds  arxiv_api_requests x 3 s + arxiv_download_requests x
                         0.5 s: the time these requests need at the pace arXiv
                         asks for, sent one after the other (one API call every
                         3 s; downloads in bursts of 4 per second, then 1 s; see
                         rate_limiter.py)
    arxiv_pause_seconds  time every arXiv request was held back because arXiv
                         refused or did not answer (arxiv_throttle_log.csv)
    ram_*, data_dir_*    benchmark.py samples: RSS of the crawler's process
                         tree and size of the data folder
    ids, papers          work/paper_manifest.jsonl
    versions_*           last row of each version in download_log.csv
    left_*               what the last pass left to do (pass_log.csv)

Group row: counts, bytes and seconds are added up, peaks are the highest
member value and ram_mean_bytes is weighted by the number of samples. The
JSON also gives wall_seconds_longest_member: members crawl on separate
runtimes at the same time (arXiv's limit applies to each person), so the
group's data is ready after the longest crawl, while wall_seconds is the
machine time of the whole group.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

from benchmark_runs import aggregate
from common import FAILED, NOT_FOUND, PDF_ONLY, SUCCESS, DataLayout, Manifest, now_iso
from discovery import id_key
from main import LEFT_KEYS
from rate_limiter import ARXIV, ARXIV_EXPORT

VERSION_STATUSES = (SUCCESS, NOT_FOUND, PDF_ONLY, FAILED)
KAGGLE_LOG = "ref_dates_kaggle_log.csv"  # written by ref_dates_kaggle.py
CRAWLER_COMMANDS = ("main.py", "downloader.py", "references.py", "ref_dates_kaggle.py")

COLUMNS = (["member", "data_dir", "first_id", "last_id", "ids", "papers", "ids_not_found",
            "ids_api_error", "versions"] + [f"versions_{s}" for s in VERSION_STATUSES]
           + ["versions_other", "downloaded_bytes", "kept_bytes", "invocations", "wall_seconds",
              "crawler_seconds", "passes", "kaggle_dates_seconds", "seconds_per_paper",
              "ram_peak_bytes", "ram_mean_bytes", "ram_samples", "data_dir_peak_bytes",
              "data_dir_final_bytes", "arxiv_requests", "arxiv_api_requests",
              "arxiv_download_requests", "arxiv_floor_seconds",
              "arxiv_pause_seconds", "arxiv_pauses", "arxiv_isolated_failures",
              "arxiv_give_ups"] + [f"left_{k}" for k in LEFT_KEYS])
HIGHEST = {"ram_peak_bytes", "data_dir_peak_bytes"}  # the group row keeps the highest; others are added


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def run_seconds(row: dict) -> float:
    """benchmark.py's wall time, or main.py's own clock when there is none."""
    return row["crawler_seconds"] if row["wall_seconds"] is None else row["wall_seconds"]


def tidy(row: dict) -> dict:
    """Whole bytes, seconds to the millisecond."""
    for key, value in row.items():
        if isinstance(value, float):
            row[key] = int(value) if key.endswith("_bytes") else round(value, 3)
    return row


def arxiv_pauses(rows: list[dict]) -> tuple[float, Counter]:
    """Seconds every arXiv request was held back, and the events by type.

    Uses the running total each crawler process writes: a pause that only
    extends a longer one adds what it extends, not its full length. The total
    starts again from 0 in each new process."""
    seconds, previous = 0.0, 0.0
    for row in rows:
        if row.get("total_paused_seconds") in (None, ""):  # row from an older log format
            seconds += number(row.get("pause_seconds")) if row.get("event") == "pause" else 0.0
            continue
        total = number(row["total_paused_seconds"])
        seconds += total - previous if total >= previous else total
        previous = total
    return seconds, Counter(row.get("event") for row in rows)


def member_row(name: str, root: Path, warnings: list[str]) -> dict:
    layout = DataLayout(root)
    logs = layout.logs
    row: dict = {"member": name, "data_dir": str(root)}

    records = Manifest(layout.manifest).load()
    statuses = Counter(r.get("status") for r in records.values())
    ids = sorted(records, key=id_key)
    row.update(first_id=ids[0] if ids else "", last_id=ids[-1] if ids else "", ids=len(ids),
               papers=statuses.get(SUCCESS, 0), ids_not_found=statuses.get(NOT_FOUND, 0),
               ids_api_error=sum(n for s, n in statuses.items() if s not in (SUCCESS, NOT_FOUND)))

    downloads = read_csv(logs / "download_log.csv")
    last = {(r.get("arxiv_id"), r.get("version")): r for r in downloads}  # later rows win
    versions = Counter(r.get("status") for r in last.values())
    row["versions"] = len(last)
    for status in VERSION_STATUSES:
        row[f"versions_{status}"] = versions.pop(status, 0)
    row["versions_other"] = sum(versions.values())
    row["downloaded_bytes"] = sum(number(r.get("downloaded_bytes")) for r in downloads)
    row["kept_bytes"] = sum(number(r.get("kept_bytes")) for r in last.values())

    runs, summary = aggregate(logs)
    row["invocations"] = summary["invocations"]
    row["wall_seconds"] = summary["sum_measured_invocation_wall_seconds"] if runs else None
    row["ram_peak_bytes"] = summary["highest_sampled_process_rss_peak_bytes"]
    row["ram_mean_bytes"] = summary["mean_sampled_rss_bytes_across_available_rows"]
    row["ram_samples"] = summary["number_of_ram_samples"]
    row["data_dir_peak_bytes"] = summary["highest_sampled_data_directory_bytes"]
    ended = [r for r in runs if r["ended_utc"] and r["data_dir_final_bytes"] is not None]
    row["data_dir_final_bytes"] = (max(ended, key=lambda r: r["ended_utc"])["data_dir_final_bytes"]
                                   if ended else None)
    warnings += [f"{name}: {e}" for e in summary["errors"]]
    for orphan in summary["orphaned_incomplete_invocations"]:
        warnings.append(f"{name}: invocation {orphan['run_id']} has no final summary (runtime "
                        f"died?), not counted; last seen after {orphan['observed_elapsed_seconds']} s")
    for run in runs:
        if not any(c in run["command"] for c in CRAWLER_COMMANDS):
            warnings.append(f"{name}: invocation {run['run_id']} is not a crawler command but is "
                            f"counted in wall_seconds: {run['command']}")
    if not runs:
        warnings.append(f"{name}: no benchmark.py summary in {logs}; wall_seconds, RAM and disk "
                        f"are unknown (crawler_seconds is main.py's own clock)")

    passes = read_csv(logs / "pass_log.csv")
    row["passes"] = len(passes)
    row["crawler_seconds"] = sum(number(p.get("seconds")) for p in passes)
    for key in LEFT_KEYS:
        row[f"left_{key}"] = int(number(passes[-1].get(key))) if passes else 0
    row["kaggle_dates_seconds"] = sum(number(r.get("seconds")) for r in read_csv(logs / KAGGLE_LOG))
    row["seconds_per_paper"] = run_seconds(row) / row["papers"] if row["papers"] else None

    api_calls = read_csv(logs / "arxiv_api_log.csv")
    row["arxiv_api_requests"] = int(sum(number(r.get("attempts")) for r in api_calls))
    row["arxiv_download_requests"] = int(sum(number(r.get("attempts")) for r in downloads))
    row["arxiv_requests"] = row["arxiv_api_requests"] + row["arxiv_download_requests"]
    row["arxiv_floor_seconds"] = (row["arxiv_api_requests"] * ARXIV.mean_interval
                                  + row["arxiv_download_requests"] * ARXIV_EXPORT.mean_interval)
    pause, events = arxiv_pauses(read_csv(logs / "arxiv_throttle_log.csv"))
    row.update(arxiv_pause_seconds=pause, arxiv_pauses=events.get("pause", 0),
               arxiv_isolated_failures=events.get("isolated", 0),
               arxiv_give_ups=events.get("give_up", 0))
    return tidy(row)


def group_row(members: list[dict]) -> dict:
    group: dict = {"member": "group", "data_dir": ""}
    ids = [m[k] for m in members for k in ("first_id", "last_id") if m[k]]
    group["first_id"] = min(ids, key=id_key) if ids else ""
    group["last_id"] = max(ids, key=id_key) if ids else ""
    for key in COLUMNS[COLUMNS.index("ids"):]:
        values = [m[key] for m in members if m[key] is not None]
        group[key] = (max(values) if key in HIGHEST else sum(values)) if values else None
    weighted = [(m["ram_mean_bytes"], m["ram_samples"]) for m in members if m["ram_mean_bytes"]]
    samples = sum(n for _, n in weighted)
    group["ram_mean_bytes"] = sum(v * n for v, n in weighted) / samples if samples else None
    if any(m["wall_seconds"] is None for m in members):
        group["wall_seconds"] = None  # unknown: compare every member on main.py's clock instead
    group["seconds_per_paper"] = run_seconds(group) / group["papers"] if group["papers"] else None
    return tidy(group)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Put the benchmark logs of every member into one table")
    parser.add_argument("folders", nargs="+", metavar="[NAME=]DATA_DIR",
                        help="a member's --data-dir, e.g. m1=/content/drive/MyDrive/g11/m1")
    parser.add_argument("--out", type=Path, default=Path("group_benchmark"),
                        help="output folder (default: group_benchmark)")
    args = parser.parse_args(argv)
    args.members = []
    for item in args.folders:
        name, _, path = item.partition("=") if "=" in item else ("", "", item)
        root = Path(path)
        if not (root / "logs").is_dir() and not (root / "work").is_dir():
            parser.error(f"{root} has no logs/ or work/ folder: is it a --data-dir?")
        args.members.append((name or root.name, root))
    return args


def hours(seconds) -> str:
    return "?" if seconds is None else f"{seconds / 3600:.2f} h"


def main(argv=None) -> int:
    args = parse_args(argv)
    warnings: list[str] = []
    members = [member_row(name, root, warnings) for name, root in args.members]
    group = group_row(members)
    rows = members + [group]

    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "group_benchmark.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    clock = "crawler_seconds" if group["wall_seconds"] is None else "wall_seconds"
    longest = max(m[clock] for m in members)
    report = {
        "generated_at": now_iso(), "members": members, "group": group,
        "wall_seconds_longest_member": longest, "clock": clock,
        "definitions": {
            "wall_seconds": "group: machine time, the sum of every member's benchmark.py "
                            "invocations (empty when a member has none)",
            "wall_seconds_longest_member": "members crawl at the same time on separate runtimes: "
                                           "the group's data is ready after this time",
            "arxiv_floor_seconds": f"arxiv_api_requests x {ARXIV.mean_interval:g} s + "
                                   f"arxiv_download_requests x {ARXIV_EXPORT.mean_interval:g} s: "
                                   f"the time the requests that were sent need at the pace arXiv "
                                   f"asks for, one after the other",
            "arxiv_pause_seconds": "time arXiv requests were held back after refusals",
            "seconds_per_paper": "wall_seconds / papers (crawler_seconds / papers when "
                                 "wall_seconds is unknown)",
            "clock": "wall_seconds, or crawler_seconds when a member has no benchmark.py summary",
        },
        "warnings": warnings,
    }
    (args.out / "group_benchmark.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")

    for row in rows:
        per_paper = f"{row[clock] / row['papers']:.1f} s" if row["papers"] else "?"
        ram = "?" if row["ram_peak_bytes"] is None else f"{row['ram_peak_bytes'] / 2**20:.0f} MB"
        print(f"{row['member']:>8}: {row['papers']} papers, {row['versions']} versions | "
              f"{'wall' if clock == 'wall_seconds' else 'main.py clock'} {hours(row[clock])} "
              f"({per_paper}/paper) | RAM peak {ram} | "
              f"arXiv: {row['arxiv_requests']} requests (floor {hours(row['arxiv_floor_seconds'])}), "
              f"paused {row['arxiv_pause_seconds'] / 60:.1f} min")
    print(f"Group data ready after the longest member: {hours(longest)}")
    for warning in warnings:
        print(f"WARNING {warning}")
    print(f"Outputs: {args.out / 'group_benchmark.csv'}, {args.out / 'group_benchmark.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
