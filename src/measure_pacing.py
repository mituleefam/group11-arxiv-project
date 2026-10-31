"""Is the source download held back by arXiv's pace, or by the network?

Sending requests through several IPs (proxies, WARP) can only speed the crawl
up if the requests wait for their turn in the shared limiter (ARXIV_EXPORT,
2 requests per second on average, at most 6 open at once; see
rate_limiter.py). If they mostly wait for the bytes to arrive, the network is
the limit and more routes change little. This tool tells the two apart.

    python src/measure_pacing.py logs  --data-dir /content/g11/AQuan_2605-12338_2605-17337
    python src/measure_pacing.py probe --start 2605.12338 --count 40 --out pacing_probe.csv

logs: reads the logs of a crawl already done (download_log.csv, run_log.csv,
arxiv_throttle_log.csv) and compares the versions downloaded per second with
the limiter's pace. It sends no request.

probe: downloads the v1 source of `count` consecutive IDs (deleted right
away) with as many workers as ARXIV_EXPORT has connections, as downloader.py does, and times
each request in three parts: waiting for a free connection and for its turn
(inside the limiter), and the transfer itself. Routes (Colab's network, WARP
when it runs) are used in turn and named in the output, so their speeds can
be compared. It keeps arXiv's pace: the limiter is the crawler's own.

What the numbers mean:
  * turn wait high, transfer short: the pace is the limit; a limiter per
    route (rate_limiter.py) would raise the download rate up to routes x pace.
  * turn wait low, transfer long: the connections are busy transferring;
    the network is the limit, and more routes help only with more connections
    and only if each route brings bandwidth of its own.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

import rate_limiter as rl
from downloader import CHUNK, CONNECT_TIMEOUT, READ_TIMEOUT, SRC_URL
from discovery import RangeBound, make_id

PROBE_FIELDS = ["versioned_id", "route", "http_status", "bytes", "wait_seconds",
                "transfer_seconds", "mb_per_second", "started", "error"]


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def describe(values: list[float], unit: str) -> str:
    if not values:
        return "-"
    values = sorted(values)
    p90 = values[min(len(values) - 1, int(0.9 * len(values)))]
    return (f"mean {statistics.mean(values):.2f}{unit}, median {statistics.median(values):.2f}{unit}, "
            f"p90 {p90:.2f}{unit}")


def from_logs(data_dir: Path) -> int:
    logs = data_dir / "logs"
    stages = [r for r in read_csv(logs / "run_log.csv") if r.get("stage") == "download"]
    downloads = read_csv(logs / "download_log.csv")
    if not stages or not downloads:
        print(f"No download stage in {logs}/run_log.csv or no rows in download_log.csv")
        return 1
    stage_seconds = sum(number(r.get("seconds")) or 0 for r in stages)
    requests = sum(number(r.get("attempts")) or 0 for r in downloads)
    fetched = [r for r in downloads if (number(r.get("downloaded_bytes")) or 0) > 0]
    pauses = rl_pause_seconds(read_csv(logs / "arxiv_throttle_log.csv"))
    active = max(stage_seconds - pauses, 1e-9)
    pace = 1 / rl.ARXIV_EXPORT.mean_interval
    rate = requests / active
    per_version = [number(r.get("download_seconds")) for r in fetched]
    per_version = [s for s in per_version if s is not None]
    mb = [(number(r.get("downloaded_bytes")) or 0) / 2**20 for r in fetched]

    print(f"Download stage: {stage_seconds / 3600:.2f} h over {len(stages)} runs, "
          f"of which arXiv pauses (every request held back) {pauses / 60:.1f} min")
    print(f"Requests to /src/: {requests:.0f} ({len(fetched)} versions with bytes)")
    print(f"Request rate outside pauses: {rate:.2f}/s; the limiter allows {pace:.2f}/s "
          f"-> {rate / pace:.0%} of the pace")
    print(f"Per version, from asking to saved (turn wait + transfer): {describe(per_version, ' s')}")
    print(f"Size per version: {describe(mb, ' MB')}")
    if rate >= 0.8 * pace:
        print("=> The download ran close to the limiter's pace: the pace is the limit. "
              "A limiter per route would raise it.")
    else:
        print("=> The download ran well below the pace: the requests were not waiting for their "
              "turn but for the network (or for retries). Run `probe` to see where the time goes.")
    return 0


def rl_pause_seconds(rows: list[dict]) -> float:
    """Seconds every arXiv request was held back (running totals per process)."""
    seconds, previous = 0.0, 0.0
    for row in rows:
        total = number(row.get("total_paused_seconds"))
        if total is None:
            continue
        seconds += total - previous if total >= previous else total
        previous = total
    return seconds


def probe(start: str, count: int, out: Path, workers: int) -> int:
    first = RangeBound.parse(start)
    ids = [make_id(first.yymm, first.number + i) + "v1" for i in range(count)]
    routes = list(rl._UNLIMITED_LIST)  # '' = Colab's own network, then WARP when it runs
    names = {p: "direct" if not p else "warp" if str(rl.WARP_PORT) in p else f"proxy{i}"
             for i, p in enumerate(routes)}
    print(f"Probe: {count} files from {ids[0]}, {workers} workers, routes: "
          f"{', '.join(names[p] for p in routes)}; limiter {rl.ARXIV_EXPORT.connections} connections, "
          f"{1 / rl.ARXIV_EXPORT.mean_interval:.1f} requests/s", flush=True)
    session = rl.make_session()
    rows: list[dict] = []
    lock = threading.Lock()
    todo = list(enumerate(ids))
    folder = Path(tempfile.mkdtemp(prefix="pacing_"))
    began = time.perf_counter()

    def work():
        while True:
            with lock:
                if not todo:
                    return
                index, versioned = todo.pop(0)
            route = routes[index % len(routes)]
            proxies = {"http": route, "https": route} if route else None
            asked = time.perf_counter()
            status, size, error, entered = None, 0, "", None
            with rl.ARXIV_EXPORT:
                entered = time.perf_counter()
                try:
                    with session.get(SRC_URL.format(versioned_id=versioned), stream=True,
                                     proxies=proxies, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as response:
                        status = response.status_code
                        if status == 200:
                            target = folder / versioned
                            with open(target, "wb") as f:
                                for chunk in response.raw.stream(CHUNK, decode_content=False):
                                    f.write(chunk)
                                    size += len(chunk)
                            target.unlink(missing_ok=True)
                except Exception as exc:  # timing tool: record and go on
                    error = f"{type(exc).__name__}: {exc}"[:200]
            done = time.perf_counter()
            transfer = done - entered
            row = {"versioned_id": versioned, "route": names[route], "http_status": status,
                   "bytes": size, "wait_seconds": round(entered - asked, 3),
                   "transfer_seconds": round(transfer, 3),
                   "mb_per_second": round(size / 2**20 / transfer, 3) if size and transfer else "",
                   "started": round(entered - began, 3), "error": error}
            with lock:
                rows.append(row)
                print(f"  {len(rows):3d}/{count} {versioned} {row['route']:>10} HTTP {status} "
                      f"{size / 2**20:6.2f} MB  wait {row['wait_seconds']:5.2f} s  "
                      f"transfer {row['transfer_seconds']:6.2f} s", flush=True)

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - began
    shutil.rmtree(folder, ignore_errors=True)

    rows.sort(key=lambda r: r["started"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PROBE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    ok = [r for r in rows if r["http_status"] == 200 and r["bytes"]]
    wait = [r["wait_seconds"] for r in rows]
    transfer = [r["transfer_seconds"] for r in ok]
    pace = 1 / rl.ARXIV_EXPORT.mean_interval
    by_connections = rl.ARXIV_EXPORT.connections / statistics.mean(transfer) if transfer else 0
    refused = [r for r in rows if r["http_status"] in (429, 503)]
    print(f"\n{len(rows)} requests in {wall:.0f} s = {len(rows) / wall:.2f}/s "
          f"({len(ok)} files, {sum(r['bytes'] for r in ok) / 2**20:.0f} MB); refused by arXiv "
          f"(429/503): {len(refused)}, each one pausing every request (15 s, then doubled)")
    print(f"Waiting inside the limiter (free connection + turn + pauses): {describe(wait, ' s')}")
    print(f"Transfer of a file: {describe(transfer, ' s')}")
    for route in sorted({r["route"] for r in rows}):
        speeds = [r["mb_per_second"] for r in ok if r["route"] == route and r["mb_per_second"] != ""]
        times = [r["transfer_seconds"] for r in ok if r["route"] == route]
        print(f"  {route:>10}: {len(times)} files, transfer {describe(times, ' s')}, "
              f"{describe(speeds, ' MB/s')}, refused {sum(1 for r in refused if r['route'] == route)}")
    print(f"Ceiling set by the pace: {pace:.2f}/s; by {rl.ARXIV_EXPORT.connections} connections "
          f"at this transfer time: {by_connections:.2f}/s")
    if len(refused) >= 0.2 * len(rows):
        print("=> arXiv's refusals are the limit: most of the waiting is the pause after a 429/503. "
              "With WARP on, a refused route pauses alone and the other goes on; compare the "
              "refusals of each route (every route refused at once = arXiv overloaded, more "
              "routes do not help).")
    elif by_connections >= pace:
        print("=> The pace is the limit (the connections could carry more). A limiter per route "
              "would raise the download rate.")
    else:
        print(f"=> The connections are the limit: files take long enough to arrive that "
              f"{rl.ARXIV_EXPORT.connections} open downloads cannot reach the pace. More routes help only with more connections, "
              "and only if each route has bandwidth of its own (compare the routes above).")
    print(f"Rows: {out}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Tell whether the download waits for arXiv's pace or for the network")
    sub = parser.add_subparsers(dest="mode", required=True)
    logs = sub.add_parser("logs", help="analyze the logs of a crawl already done (no request)")
    logs.add_argument("--data-dir", type=Path, required=True)
    p = sub.add_parser("probe", help="time a few downloads at arXiv's pace")
    p.add_argument("--start", required=True, help="first ID, e.g. 2605.12338")
    p.add_argument("--count", type=int, default=40)
    p.add_argument("--workers", type=int, default=rl.ARXIV_EXPORT.connections)
    p.add_argument("--out", type=Path, default=Path("pacing_probe.csv"))
    args = parser.parse_args(argv)
    if args.mode == "logs":
        return from_logs(args.data_dir)
    return probe(args.start, args.count, args.out, args.workers)


if __name__ == "__main__":
    sys.exit(main())
