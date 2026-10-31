"""Shared, polite access to arXiv with Proxy Rotation support.

arXiv publishes two paces, and every module goes through the limiter of the
pace that covers its requests instead of sleeping on its own:

  * ARXIV, for the arXiv API (discovery, metadata, reference dates). Terms of
    Use (https://info.arxiv.org/help/api/tou.html, "Rate limits"): "When using
    the legacy APIs (including OAI-PMH, RSS, and the arXiv API), make no more
    than one request every three seconds, and limit requests to a single
    connection at a time."
  * ARXIV_EXPORT, for every request sent to arXiv: the source downloads
    (/src/), and the API calls too (ARXIV enters it), so that all requests
    together stay within this pace. Bulk data page, "Custom Programmatic
    Harvesting" (https://info.arxiv.org/help/bulk_data.html#harvest): use
    "the dedicated site export.arxiv.org", and "we suggest that a *reasonable
    rate* to be bursts at 4 requests per second with a 1 second `sleep`, per
    burst". Read here as: at most 4 requests start, 0.25 s apart, then none
    for 1 s, i.e. 2 requests per second on average, with up to 6 downloads
    open at once (a source file often takes several seconds to arrive, so 4
    open downloads could not keep this pace; the page sets no connection
    limit, and more connections do not start more requests per second).

The Terms add that the rate limits "apply to all of the machines under your
control as a whole", that "You should not attempt to overcome these limits by
increasing the number of machines used to make requests", and list "Attempt
to circumvent rate limits" under "Things that you must not do"; a higher rate
is asked from arXiv support.

arXiv may still answer HTTP 429 ("too many requests") or HTTP 503 ("service
unavailable"). arXiv staff explain that "Rate exceeded" (429) "relates to
server capacity and total number of users, not to individual usage", that a
503 is what excessive use gets, and that "the only fix is try again later"
(arXiv API group, 2025-11-17, https://groups.google.com/a/arxiv.org/g/api/c/pNB3lnxf4mQ).
ARXIV_EXPORT handles these refusals for every arXiv request.

Waiting counts in the measured runtime, so the limiter waits only when it
helps, and only as long as needed (exponential backoff with a cap, the same
principle as the tenacity and backoff libraries; the pause here is shared by
every module and thread, which those per-call decorators do not provide):

  * HTTP 429 (arXiv overloaded): every request would be refused, so *every*
    arXiv request pauses: 15 s, doubled each time arXiv refuses again after a
    pause, up to 5 minutes, or longer if the server sends Retry-After. Several
    downloads are open at once, so one refusal often comes back on several of
    them: refusals that arrive during a pause belong to the same episode and
    do not double it again. The pace is not slowed afterwards, since the cause
    is the total load, not our rate.
  * HTTP 503 (excessive use) or no answer at all (network down, DNS failure,
    server silent): a single failing file is only retried by its caller, while
    the other downloads go on; it does not stop the crawl. When two different
    URLs fail within 2 minutes the problem is general: every request pauses as
    above, and after a 503 requests are spaced 6 s apart until 30 in a row
    have succeeded.
  * A source file (/src/) that fails again (HTTP 429 or 503, or no answer)
    after an earlier failure in this run, or that already failed in an
    earlier pass or run (the downloader says so with mark_suspect()), has a
    problem of its own: arXiv can be unable to serve one source for hours
    while it serves every other one (seen in our October 2026 crawls: a few
    versions answered 503 and 429 for hours, and each time two of them failed
    close together every request paused, about 30 minutes in all). Such a
    failure pauses nothing and does not make the next failure look general.
    After `defer_after` failures (one, for a file that failed in an earlier
    pass) should_defer() tells the downloader to put the file aside for a
    later pass instead of asking for it again and again. When arXiv refuses
    every request, each new file it refuses still pauses every request, so a
    general refusal keeps the doubling pauses and the give-up below, while
    the files it refused are asked for at most twice. API calls keep the two
    rules above.
  * Any normal answer ends a run of refusals.

Routes (Colab's own network, WARP, the Webshare proxies; see ProxyPool):
when a refusal above would pause every request, and the request went
through a route while another route that serves the same kind of request
(source files: Colab and WARP; API calls: every route) is not paused, only
that route pauses instead: 15 s, doubled at each new refusal of that route
up to 5 minutes, back to 15 s after it serves a request. ProxyPool skips a
paused route, so the other routes go on at the same shared pace (the routes
never add up to more than the pace above). The refusal that leaves no other
route pauses every request as above. Without WARP or proxies there is one
route, and every refusal pauses every request, as before. Route pauses are
logged as "route_pause" in arxiv_throttle_log.csv; they do not count in
total_paused_seconds, since requests went on through the other routes.

arXiv never says how long it will keep refusing. The pauses are therefore not
a guess of when the refusal ends, but how often requests check whether it has
ended: about 0.25, 0.75, 1.75, 3.75, 7.75 and 12.75 minutes after the first
refusal, then every 5 minutes. If arXiv has refused or not answered every
request for `give_up_after` seconds (30 minutes by default, set with main.py
--max-wait-minutes), ArxivUnavailable is raised: the crawl stops instead of
waiting for hours, and everything finished so far stays saved.

Sessions from make_session() report every arXiv answer and every failed
connection to `ARXIV_EXPORT` (a response hook and a transport adapter), so
the modules that send the requests need no change.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

DEFAULT_USER_AGENT = "NMKHDL-arxiv-crawler/1.0 (university course project)"

# Status codes worth retrying: rate limited or a temporary server problem.
RETRY_STATUS = {429, 500, 502, 503, 504}

# Answers from these hosts are reported to ARXIV_EXPORT (see make_session).
ARXIV_HOSTS = ("arxiv.org",)

# Columns of logs/arxiv_throttle_log.csv: one row per pause or recovery, so the
# report can tell time lost to arXiv refusals from the crawler's own time.
THROTTLE_LOG_FIELDS = [
    "time", "event", "reason", "pause_seconds",
    "total_paused_seconds", "interval_seconds", "url"
]

WARP_HOST = "127.0.0.1"
WARP_PORT = int(os.environ.get("WARP_PORT", "40000"))
PROXIES_FILE = Path(__file__).resolve().parent / "proxies.txt"


def _is_warp_running(host: str = WARP_HOST, port: int = WARP_PORT) -> bool:
    """Tự động kiểm tra xem Cloudflare WARP (Proxy mode) có đang bật trên máy không."""
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _socks_available() -> bool:
    """socks5h:// (WARP) cần PySocks: pip install "requests[socks]"."""
    try:
        import socks  # noqa: F401  (PySocks)
        return True
    except ImportError:
        return False


def _mask(proxy: str) -> str:
    """Che user:pass khi in proxy ra log."""
    return re.sub(r"//[^@/]*@", "//***@", proxy) if proxy else "mạng gốc"


# Proxy/WARP vừa lỗi kết nối: tạm bỏ qua trong QUARANTINE_SECONDS rồi thử lại.
# Lỗi của proxy không phải lỗi của arXiv, nên không báo về ARXIV_EXPORT (không dừng cả crawler).
QUARANTINE_SECONDS = 300.0
_bad_until: dict[str, float] = {}
_bad_lock = threading.Lock()


def _quarantine(proxy: str, reason: str) -> None:
    with _bad_lock:
        now = time.monotonic()
        new = _bad_until.get(proxy, 0.0) <= now
        _bad_until[proxy] = now + QUARANTINE_SECONDS
    if new:
        print(f"[proxy] {_mask(proxy)}: {reason}, bỏ qua {QUARANTINE_SECONDS / 60:g} phút "
              f"(request được thử lại qua đường kế tiếp; không tính là arXiv từ chối)", flush=True)


# Routes refused by arXiv (HTTP 429, or 503 on several files): skipped by ProxyPool until
# then, while the other routes go on (see "Routes" above). '' is Colab's own network.
ROUTE_COOLDOWN, ROUTE_MAX_COOLDOWN = 15.0, 300.0
_route_paused_until: dict[str, float] = {}
_route_next_cooldown: dict[str, float] = {}


def _route_name(route: str) -> str:
    if not route:
        return "Colab"
    if route.endswith(f"{WARP_HOST}:{WARP_PORT}"):
        return "WARP"
    return _mask(route)


def _pause_route(route: str, url: str, retry_after: float) -> float | None:
    """Pause `route` alone when another route that serves `url` is not
    paused; returns the pause in seconds (0 when it was already paused), or
    None when no route would be left (the caller then pauses every request)."""
    routes = _UNLIMITED_LIST if _is_source(url) else PROXIES
    with _bad_lock:
        now = time.monotonic()
        others = [r for r in routes if r != route and _route_paused_until.get(r, 0.0) <= now
                  and (not r or _bad_until.get(r, 0.0) <= now)]
        if not others:
            return None
        if _route_paused_until.get(route, 0.0) > now:  # same episode: do not double again
            _route_paused_until[route] = max(_route_paused_until[route], now + retry_after)
            return 0.0
        pause = max(_route_next_cooldown.get(route, ROUTE_COOLDOWN), retry_after)
        _route_paused_until[route] = now + pause
        _route_next_cooldown[route] = min(pause * 2, ROUTE_MAX_COOLDOWN)
    return pause


def _route_served(route: str) -> None:
    """`route` got a normal answer: its next refusal pauses it for 15 s again."""
    if route in _route_next_cooldown:
        with _bad_lock:
            _route_next_cooldown.pop(route, None)


def _is_source(url: str) -> bool:
    return "/src/" in url or "/pdf/" in url or "/e-print/" in url


def _has_explicit_proxy(proxies) -> bool:
    """True khi request đã có proxy thật cho http/https (người gọi truyền vào, hoặc HTTPS_PROXY
    của máy). Dict chỉ có khoá lạ như 'no' (từ NO_PROXY) không tính, nên vẫn xoay được."""
    if not proxies:
        return False
    return any(v for k, v in proxies.items() if k.split(":")[0] in ("http", "https", "all"))


def _load_external_proxies() -> list[str]:
    """Đọc danh sách proxy từ biến môi trường ARXIV_PROXIES hoặc file proxies.txt."""
    env_val = os.environ.get("ARXIV_PROXIES", "")
    if env_val.strip():
        return [p.strip() for p in env_val.split(",") if p.strip()]
    if PROXIES_FILE.is_file():
        lines = PROXIES_FILE.read_text(encoding="utf-8").splitlines()
        return [
            line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
    return []


class ProxyPool:
    """Xoay vòng: mỗi request lấy proxy kế tiếp trong danh sách (thread-safe),
    bỏ qua proxy đang tạm nghỉ vì vừa lỗi kết nối, và đường (kể cả '' = mạng gốc)
    đang tạm dừng vì arXiv vừa từ chối nó."""

    def __init__(self, proxy_urls: list[str] | None = None):
        self._proxies = list(proxy_urls) if proxy_urls is not None else []
        self._i = 0
        self._lock = threading.Lock()

    def next(self) -> str | None:
        """Trả về URL proxy kế tiếp còn dùng được ('' = mạng gốc), hoặc None nếu danh sách rỗng."""
        if not self._proxies:
            return None
        with self._lock:
            now = time.monotonic()
            for _ in range(len(self._proxies)):
                proxy = self._proxies[self._i % len(self._proxies)]
                self._i += 1
                if _route_paused_until.get(proxy, 0.0) > now:
                    continue
                if not proxy or _bad_until.get(proxy, 0.0) <= now:
                    return proxy
            # mọi đường đang nghỉ: đường hết dừng sớm nhất (cùng lúc thì mạng gốc)
            return min(self._proxies, key=lambda p: (_route_paused_until.get(p, 0.0), bool(p)))

    def next_proxies_dict(self) -> dict[str, str] | None:
        """Trả về dict proxies chuẩn cho thư viện requests (chuỗi '' = dùng mạng gốc)."""
        proxy = self.next()
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    def __bool__(self) -> bool:
        return bool(self._proxies)


# 1. Danh sách không giới hạn dung lượng (Mạng gốc + WARP nếu máy có bật) -> Dùng tải /src/, /pdf/
_UNLIMITED_LIST: list[str] = [""]
if _is_warp_running():
    if _socks_available():
        _UNLIMITED_LIST.append(f"socks5h://{WARP_HOST}:{WARP_PORT}")
    else:
        print(f'[proxy] WARP đang mở ở {WARP_HOST}:{WARP_PORT} nhưng thiếu PySocks '
              f'(pip install "requests[socks]"): bỏ qua WARP', flush=True)

# 2. Danh sách cho API nhẹ -> Gồm _UNLIMITED_LIST + 10 IP Webshare đọc từ proxies.txt
PROXIES: list[str] = _UNLIMITED_LIST + _load_external_proxies()

PROXY_POOL = ProxyPool(PROXIES)
UNLIMITED_POOL = ProxyPool(_UNLIMITED_LIST)


class ArxivUnavailable(RuntimeError):
    """arXiv refused or did not answer every request for `give_up_after`
    seconds. It is not a requests error, so no module retries it: the crawl
    stops (main.py exits with code 3)."""


class RateLimiter:
    """Context manager that keeps request starts at least `min_interval`
    seconds apart and allows `connections` requests at a time (one by
    default).

        with ARXIV:
            response = session.get(url, timeout=60)

    The interval is counted from the start of the previous request: if a
    download already took 7 seconds, the next request starts immediately.
    With `burst` > 1, every `burst` starts are followed by `burst_pause`
    seconds without any (ARXIV_EXPORT: 4 starts 0.25 s apart, then 1 s). A
    limiter with a `parent` also enters the parent for each request, so the
    request counts in both paces (ARXIV inside ARXIV_EXPORT).

    With `cooldown` > 0 the limiter also reacts to refusals: throttled()
    pauses every request for the cool-down (and, with slow_down=True, switches
    to `slow_interval`); failed() reports a failure that may concern one URL
    only and calls throttled() when two different URLs fail within
    `isolation_window` seconds; succeeded() ends a run of refusals and brings
    the normal pace back after `recover_after` successful requests in a row.
    With the default cooldown=0 these calls do nothing. With `give_up_after`
    > 0, entering the limiter raises ArxivUnavailable once every request has
    been refused for that many seconds.

    URLs containing one of `file_markers` are files: refused() (HTTP 429) and
    failed() count their failures one by one, and a file that failed before
    only adds a failure of its own (no pause). mark_suspect() flags a file
    that failed in an earlier pass or run, should_defer() says when its
    caller should leave it for a later pass (`defer_after` failures, 0:
    never) and deferred() logs that it did.
    """

    def __init__(self, min_interval: float, *, burst: int = 1, burst_pause: float = 0.0,
                 connections: int = 1, parent: RateLimiter | None = None,
                 cooldown: float = 0.0,
                 max_cooldown: float = 0.0, slow_interval: float | None = None,
                 recover_after: int = 30, give_up_after: float = 0.0,
                 isolation_window: float = 120.0, name: str = "limiter",
                 file_markers: tuple[str, ...] = (), defer_after: int = 0):
        self.min_interval = min_interval
        self.interval = min_interval  # current spacing, slower after a general 503
        self.burst = max(burst, 1)
        self.burst_pause = burst_pause
        self.connections = max(connections, 1)
        self.parent = parent
        self.cooldown = cooldown
        self.max_cooldown = max(max_cooldown, cooldown)
        self.slow_interval = max(slow_interval or min_interval, min_interval)
        self.recover_after = recover_after
        self.give_up_after = give_up_after
        self.isolation_window = isolation_window
        self.name = name
        self.file_markers = tuple(file_markers)
        self.defer_after = defer_after
        self.log = None  # optional CsvLog with THROTTLE_LOG_FIELDS (set by main.py)
        self.throttle_count = 0      # refusals that paused every request
        self.isolated_count = 0      # failures of a single URL (no pause)
        self.deferred_count = 0      # files put aside for a later pass (deferred())
        self.paused_seconds = 0.0    # total time all requests were held back
        self._last_failure = None    # (url, time) of the latest single-URL failure
        self._file_failures: dict[str, int] = {}  # file URL -> failures since its last success
        self._suspects: set[str] = set()  # file URLs that failed in an earlier pass or run
        self._slots = threading.BoundedSemaphore(self.connections)  # requests open at once
        self._state = threading.Lock()  # pace and pause bookkeeping, held only briefly
        self._last_start = float("-inf")
        self._in_burst = 0  # requests started in the current burst
        self._paused_until = float("-inf")
        self._next_cooldown = cooldown
        self._successes = 0
        self._refused_since = None  # start of the current run of refusals
        self._gave_up = False

    def __enter__(self):
        self._slots.acquire()  # at most `connections` requests open at once
        try:
            self._wait_turn()
            self._check_give_up()
            if self.parent is not None:
                self.parent.__enter__()
        except BaseException:  # giving up, or Ctrl+C during a pause
            self._slots.release()
            raise
        if self.parent is not None:
            with self._state:
                # The parent may have made the request wait: count from its real start.
                self._last_start = max(self._last_start, time.monotonic())
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.parent is not None:
            self.parent.__exit__(exc_type, exc, tb)
        self._slots.release()
        return False

    @property
    def mean_interval(self) -> float:
        """Seconds per request on average at the normal pace (ARXIV: 3,
        ARXIV_EXPORT: 0.5)."""
        return self.min_interval + self.burst_pause / self.burst

    def _wait_turn(self) -> None:
        """Sleep until this request may start, then count its start. With
        several connections the start is checked and counted under one lock,
        so two requests never take the same turn; a pause that begins while
        a request waits (a 429 on another connection) makes it wait longer."""
        while True:
            with self._state:
                now = time.monotonic()
                gap = self.interval + (self.burst_pause if self._in_burst >= self.burst else 0.0)
                start_at = max(self._last_start + gap, self._paused_until)
                if start_at <= now:
                    if self._in_burst >= self.burst or \
                            now - self._last_start >= self.interval + self.burst_pause:
                        self._in_burst = 0  # a new burst: the last one is full, or idle long enough
                    self._in_burst += 1
                    self._last_start = now
                    return
            time.sleep(start_at - now)  # only wait for what is left of the interval or pause

    def throttled(self, retry_after: float = 0.0, url: str = "", reason: str = "HTTP 429",
                  slow_down: bool = False) -> None:
        """Every request would be refused: hold back every request for the
        current cool-down (or Retry-After when longer) and double the next
        cool-down. No request starts during a pause, so a refusal that arrives
        while one runs answers a request sent before it (several connections
        are open at once): it belongs to the same episode and can only lengthen
        the pause to a longer Retry-After. With slow_down=True, requests are
        also spaced `slow_interval` apart until `recover_after` of them have
        succeeded."""
        if not self.cooldown:
            return
        with self._state:
            now = time.monotonic()
            ongoing = now < self._paused_until  # same episode: do not double again
            pause = retry_after if ongoing else max(self._next_cooldown, retry_after)
            # Never shorten an ongoing pause; count only the time it is extended by.
            until = max(self._paused_until, now + pause)
            extended = until - max(self._paused_until, now)
            self.paused_seconds += extended
            self._paused_until = until
            if slow_down:
                self.interval = self.slow_interval
            if not ongoing:
                self._next_cooldown = min(self._next_cooldown * 2, self.max_cooldown)
                self._successes = 0
                self._last_failure = None
                self.throttle_count += 1
                if self._refused_since is None:
                    self._refused_since = now
            refused_for = now - self._refused_since if self._refused_since is not None else 0.0
            total = self.paused_seconds
        if ongoing:
            if extended > 0:
                self._log("pause", reason, extended, total, url)
            return
        so_far = f"{refused_for:.0f} s" if refused_for < 120 else f"{refused_for / 60:.0f} min"
        limit = (f"; the crawl stops if this lasts {self.give_up_after / 60:.0f} min"
                 if self.give_up_after else "")
        print(f"[{self.name}] {reason}: pausing all {self.name} requests for {pause:.0f} s "
              f"(refused for {so_far} so far{limit})", flush=True)
        self._log("pause", reason, pause, total, url)

    def refused(self, url: str, retry_after: float = 0.0, route: str | None = None) -> None:
        """HTTP 429. For a file that failed before, one more failure of that
        file (see failed()); for anything else every request would be
        refused: throttled(), or only `route` pauses when another route can
        take the requests (see "Routes" in the module docstring)."""
        if not self.cooldown:
            return
        with self._state:
            own = self._known_bad(url)
            if not own and self._is_file(url):
                self._file_failures[url] = self._file_failures.get(url, 0) + 1
        if own:
            self.failed(url, "HTTP 429", retry_after, route=route)
        elif not self._route_refused(route, url, retry_after, "HTTP 429"):
            self.throttled(retry_after, url, "HTTP 429")

    def _route_refused(self, route: str | None, url: str, retry_after: float, reason: str) -> bool:
        """Pause `route` alone if another route can take the requests;
        False when every request has to pause."""
        if route is None:
            return False
        pause = _pause_route(route, url, retry_after)
        if pause is None:
            return False
        if pause:
            print(f"[{self.name}] {reason} via {_route_name(route)}: pausing this route for "
                  f"{pause:.0f} s, the other routes go on", flush=True)
            self._log("route_pause", f"{reason} via {_route_name(route)}", pause,
                      self.paused_seconds, url)
        return True

    def failed(self, url: str, reason: str, retry_after: float = 0.0,
               slow_down: bool = False, route: str | None = None) -> None:
        """A request failed in a way that may concern this URL only (HTTP 503,
        no answer). Its caller retries it with its own backoff while the other
        requests go on. If a different URL failed too within
        `isolation_window` seconds, the problem is general: throttled(). A
        file that failed before has a problem of its own: its failure is
        only counted, and it is not compared with the other URLs."""
        if not self.cooldown:
            return
        with self._state:
            now = time.monotonic()
            own = self._known_bad(url)
            if self._is_file(url):
                self._file_failures[url] = self._file_failures.get(url, 0) + 1
            if own:
                general = new_url = False
                self.isolated_count += 1
            else:
                last = self._last_failure
                general = (last is not None and last[0] != url
                           and now - last[1] <= self.isolation_window)
                self._last_failure = (url, now)
                if not general:
                    self.isolated_count += 1
                new_url = last is None or last[0] != url
        if general:
            if not self._route_refused(route, url, retry_after, f"{reason} on several files"):
                self.throttled(retry_after, url, f"{reason} on several files", slow_down)
            return
        if new_url:
            print(f"[{self.name}] {reason} for one file only: retrying it later, "
                  f"the other requests go on", flush=True)
        self._log("isolated", reason, 0, self.paused_seconds, url)

    def _is_file(self, url: str) -> bool:
        return any(marker in url for marker in self.file_markers)

    def _known_bad(self, url: str) -> bool:
        """A file that failed earlier in this run or in an earlier pass or run
        (call with self._state held)."""
        return self._is_file(url) and (url in self._suspects or self._file_failures.get(url, 0) > 0)

    def mark_suspect(self, url: str) -> None:
        """`url` failed in an earlier pass or run: its next failure is a
        problem of its own, and one failure is enough to put it aside."""
        if self._is_file(url):
            with self._state:
                self._suspects.add(url)

    def should_defer(self, url: str) -> bool:
        """True when the caller should stop asking for `url` now and leave it
        for a later pass: a file that failed `defer_after` times in this run
        (once, if it already failed in an earlier pass)."""
        if not self.defer_after or not self._is_file(url):
            return False
        with self._state:
            failures = self._file_failures.get(url, 0)
            return failures >= self.defer_after or (url in self._suspects and failures >= 1)

    def deferred(self, url: str, reason: str) -> None:
        """The caller put `url` aside for a later pass (logged as "defer")."""
        with self._state:
            self.deferred_count += 1
            total = self.paused_seconds
        self._log("defer", reason, 0, total, url)

    def succeeded(self, url: str = "") -> None:
        """The server answered normally (for `url`, when given: that file is
        no longer suspect). After `recover_after` successes in a row the
        normal pace and the first cool-down length come back."""
        if not self.cooldown:
            return
        with self._state:
            self._refused_since = None  # any normal answer ends the run of refusals
            self._last_failure = None   # and a later single failure is isolated again
            if url:
                self._file_failures.pop(url, None)
                self._suspects.discard(url)
            if self.interval == self.min_interval and self._next_cooldown == self.cooldown:
                return  # already at the normal pace
            self._successes += 1
            if self._successes < self.recover_after:
                return
            self.interval = self.min_interval
            self._next_cooldown = self.cooldown
            self._successes = 0
            total = self.paused_seconds
        if self.burst > 1:
            pace = (f"bursts of {self.burst} requests {self.interval:g} s apart, "
                    f"then {self.burst_pause:g} s")
        else:
            pace = f"{self.interval:g} s between requests"
        print(f"[{self.name}] {self.recover_after} requests in a row succeeded: back to {pace}",
              flush=True)
        self._log("recover", "", 0, total, "")

    def _check_give_up(self) -> None:
        """Raise ArxivUnavailable when every request has been refused for
        `give_up_after` seconds (called just before a request is sent)."""
        with self._state:
            since = self._refused_since
        if not self.give_up_after or since is None:
            return
        refused_for = time.monotonic() - since
        if refused_for < self.give_up_after:
            return
        if not self._gave_up:
            self._gave_up = True
            self._log("give_up", f"refused for {refused_for / 60:.0f} min", 0, self.paused_seconds, "")
        raise ArxivUnavailable(
            f"{self.name} has refused or not answered every request for {refused_for / 60:.0f} "
            f"minutes (HTTP 429/503 or no answer), so the crawl stops here instead of waiting "
            f"longer. Everything finished so far is saved.")

    def _log(self, event: str, reason: str, pause: float, total: float, url: str) -> None:
        if self.log is not None:
            self.log.write(time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           event=event, reason=reason, pause_seconds=round(pause, 1),
                           total_paused_seconds=round(total, 1),
                           interval_seconds=self.interval, url=url)


# Every request sent to arXiv: /src/ downloads directly, API calls through
# ARXIV. Pace of the bulk data page; refusals pause every arXiv request.
ARXIV_EXPORT = RateLimiter(min_interval=0.25, burst=4, burst_pause=1.0, connections=6,
                           cooldown=15.0, max_cooldown=300.0, slow_interval=6.0,
                           recover_after=30, give_up_after=1800.0,
                           isolation_window=120.0, name="arXiv",
                           file_markers=("/src/", "/e-print/"), defer_after=2)

# arXiv API calls (discovery, metadata, reference dates): 3 s apart, one at a time.
ARXIV = RateLimiter(min_interval=3.0, parent=ARXIV_EXPORT, name="arXiv API")


def _is_arxiv(url: str) -> bool:
    """True for arXiv URLs; other servers (Semantic Scholar has its own
    limiter and retries) are never reported to ARXIV_EXPORT."""
    host = urlsplit(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in ARXIV_HOSTS)


def _report_to_arxiv_limiter(response, *args, **kwargs):
    """requests response hook: tell ARXIV_EXPORT how arXiv answered, and
    through which route (set by _ArxivAdapter)."""
    if _is_arxiv(response.url):
        status = response.status_code
        route = getattr(response, "arxiv_route", None)
        if status == 429:    # overloaded for everyone (or a file that keeps failing)
            ARXIV_EXPORT.refused(response.url, _retry_after_seconds(response), route=route)
        elif status == 503:  # this file, or excessive use if it spreads
            ARXIV_EXPORT.failed(response.url, "HTTP 503", _retry_after_seconds(response),
                                slow_down=True, route=route)
        elif status < 400:
            ARXIV_EXPORT.succeeded(response.url)
            if route is not None:
                _route_served(route)
    return None  # keep the response unchanged


class _ArxivAdapter(HTTPAdapter):
    """Transport adapter that also reports to ARXIV_EXPORT when arXiv gives no
    answer at all (network down, DNS failure, no reply in time). One silent
    request is retried by its caller; when different URLs keep failing, every
    request waits for the cool-down instead of failing one after the other. A
    connection cut while a body is being read is not reported: that error
    comes later, from reading the body, and downloader.py resumes the file.

    Transport adapter tự động xoay vòng proxy theo loại request và báo lỗi kết nối
    về ARXIV_EXPORT."""

    def __init__(self, proxy_pool: ProxyPool | None = PROXY_POOL, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_pool = proxy_pool

    def send(self, request, *args, **kwargs):
        chosen = ""  # proxy dùng cho request này; '' = mạng gốc
        route = None  # đường của request, để limiter dừng riêng đường bị arXiv từ chối
        if _is_arxiv(request.url):
            explicit = kwargs.get("proxies")
            if _has_explicit_proxy(explicit):
                route = explicit.get("https") or explicit.get("http") or ""
            else:
                if _is_source(request.url):
                    chosen = UNLIMITED_POOL.next() or ""
                elif self.proxy_pool:
                    chosen = self.proxy_pool.next() or ""
                route = chosen
            if chosen:
                kwargs["proxies"] = {"http": chosen, "https": chosen}

        try:
            response = super().send(request, *args, **kwargs)
            response.arxiv_route = route  # read by _report_to_arxiv_limiter
            return response
        except (requests.ConnectionError, requests.Timeout) as exc:
            if chosen:  # proxy/WARP hỏng hoặc hết băng thông: tạm bỏ nó, người gọi thử lại đường khác
                _quarantine(chosen, f"lỗi kết nối ({type(exc).__name__})")
            elif _is_arxiv(request.url):
                ARXIV_EXPORT.failed(request.url, f"no answer ({type(exc).__name__})", route=route)
            raise


def make_session(user_agent: str = DEFAULT_USER_AGENT,
                 proxy_pool: ProxyPool | None = PROXY_POOL) -> requests.Session:
    """HTTP session that identifies the crawler, as arXiv asks, and reports
    arXiv's answers and failed connections to the shared ARXIV_EXPORT limiter.

    HTTP session tự động gắn User-Agent, xoay vòng proxy qua _ArxivAdapter,
    và báo trạng thái phản hồi về ARXIV_EXPORT limiter."""
    session = requests.Session()
    session.headers["User-Agent"] = user_agent
    session.hooks["response"].append(_report_to_arxiv_limiter)
    adapter = _ArxivAdapter(proxy_pool=proxy_pool)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_with_retry(session: requests.Session, url: str, *, params=None,
                   limiter: RateLimiter = ARXIV, timeout: float = 60,
                   max_attempts: int = 5, first_backoff: float = 3.0):
    """GET `url` through `limiter`, retrying transient failures.

    Network errors and HTTP 429/5xx are retried after 3, 6, 12, 24 seconds
    (or the server's Retry-After when it is longer; after a 429 the limiter's
    cool-down also applies). The whole response body is read while the
    limiter is held, so ARXIV never has more than one connection open.

    Returns (response, attempts, error): `response` is the last response
    received (None after a network error) and `error` is "" on HTTP 200.

    Mỗi lần retry khi đi qua `session.get()` sẽ tự động được `_ArxivAdapter`
    cấp một proxy kế tiếp trong pool.
    """
    response, error = None, ""
    backoff = first_backoff
    for attempt in range(1, max_attempts + 1):
        with limiter:
            try:
                response = session.get(url, params=params, timeout=timeout)
                error = "" if response.status_code == 200 else f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                response, error = None, f"{type(exc).__name__}: {exc}"
        if not error:
            return response, attempt, ""
        if response is not None and response.status_code not in RETRY_STATUS:
            return response, attempt, error  # e.g. 404: retrying will not help
        if attempt < max_attempts:
            time.sleep(max(backoff, _retry_after_seconds(response)))
            backoff *= 2
    return response, max_attempts, error


def _retry_after_seconds(response) -> float:
    if response is None:
        return 0.0
    try:
        return float(response.headers.get("Retry-After", 0))
    except ValueError:
        return 0.0
