"""Shared, polite access to arXiv.

arXiv asks for at least 3 seconds between requests and only one connection at
a time. The limit applies to the whole program, so every module that calls
arXiv (discovery, metadata, source download, reference dates) must go through
the same `ARXIV` limiter instead of sleeping on its own.

arXiv may still answer HTTP 429 ("too many requests"), for example when a
Colab machine shares its IP address with other users. Retrying one request
is not enough: the next download would start at once, be refused too, and a
whole chain of versions would fail. So on a 429 the limiter pauses *every*
arXiv request for a cool-down (60 s, doubled at each new 429 up to 5 minutes,
longer if the server sends Retry-After) and spaces requests 6 s apart until
30 requests in a row have succeeded. When arXiv never answers 429, nothing
changes: requests stay 3 s apart.

Sessions from make_session() report every arXiv answer to `ARXIV` through a
response hook, so the modules that send the requests need no change.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

DEFAULT_USER_AGENT = "NMKHDL-arxiv-crawler/1.0 (university course project)"

# Status codes worth retrying: rate limited or a temporary server problem.
RETRY_STATUS = {429, 500, 502, 503, 504}

# Answers from these hosts are reported to ARXIV (see make_session).
ARXIV_HOSTS = ("arxiv.org",)

# Columns of logs/arxiv_throttle_log.csv: one row per pause or recovery, so the
# report can tell time lost to arXiv throttling from the crawler's own time.
THROTTLE_LOG_FIELDS = ["time", "event", "pause_seconds", "total_paused_seconds",
                       "interval_seconds", "url"]


class RateLimiter:
    """Context manager that allows one request at a time and keeps request
    starts at least `min_interval` seconds apart.

        with ARXIV:
            response = session.get(url, timeout=60)

    The interval is counted from the start of the previous request: if a
    download already took 7 seconds, the next request starts immediately.

    With `cooldown` > 0 the limiter also reacts to HTTP 429: throttled()
    pauses every request for the cool-down and switches to `slow_interval`;
    succeeded() brings the normal pace back after `recover_after` successful
    requests in a row. With the default cooldown=0 both calls do nothing.
    """

    def __init__(self, min_interval: float, *, cooldown: float = 0.0,
                 max_cooldown: float = 0.0, slow_interval: float | None = None,
                 recover_after: int = 30, name: str = "limiter"):
        self.min_interval = min_interval
        self.interval = min_interval  # current spacing, slower after a 429
        self.cooldown = cooldown
        self.max_cooldown = max(max_cooldown, cooldown)
        self.slow_interval = max(slow_interval or min_interval, min_interval)
        self.recover_after = recover_after
        self.name = name
        self.log = None  # optional CsvLog with THROTTLE_LOG_FIELDS (set by main.py)
        self.throttle_count = 0      # HTTP 429 answers seen
        self.paused_seconds = 0.0    # total time all requests were held back
        self._lock = threading.Lock()   # one request at a time
        self._state = threading.Lock()  # pause bookkeeping, held only briefly
        self._last_start = float("-inf")
        self._paused_until = float("-inf")
        self._next_cooldown = cooldown
        self._successes = 0

    def __enter__(self):
        self._lock.acquire()  # one connection at a time
        with self._state:
            start_at = max(self._last_start + self.interval, self._paused_until)
        wait = start_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)  # only wait for what is left of the interval or pause
        self._last_start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False

    def throttled(self, retry_after: float = 0.0, url: str = "") -> None:
        """The server answered HTTP 429: hold back every request for the
        current cool-down (or Retry-After when longer), double the next
        cool-down and slow the pace until requests succeed again."""
        if not self.cooldown:
            return
        with self._state:
            now = time.monotonic()
            pause = max(self._next_cooldown, retry_after)
            # Never shorten an ongoing pause; count only the time it is extended by.
            until = max(self._paused_until, now + pause)
            self.paused_seconds += until - max(self._paused_until, now)
            self._paused_until = until
            self._next_cooldown = min(self._next_cooldown * 2, self.max_cooldown)
            self.interval = self.slow_interval
            self._successes = 0
            self.throttle_count += 1
            total = self.paused_seconds
        print(f"[{self.name}] HTTP 429: pausing all {self.name} requests for {pause:.0f} s, "
              f"then {self.interval:.0f} s between requests", flush=True)
        self._log("pause", pause, total, url)

    def succeeded(self) -> None:
        """The server answered normally. After `recover_after` successes in a
        row the normal pace and the first cool-down length come back."""
        if not self.cooldown:
            return
        with self._state:
            if self.interval == self.min_interval and self._next_cooldown == self.cooldown:
                return  # already at the normal pace
            self._successes += 1
            if self._successes < self.recover_after:
                return
            self.interval = self.min_interval
            self._next_cooldown = self.cooldown
            self._successes = 0
            total = self.paused_seconds
        print(f"[{self.name}] {self.recover_after} requests in a row succeeded: "
              f"back to {self.interval:.0f} s between requests", flush=True)
        self._log("recover", 0, total, "")

    def _log(self, event: str, pause: float, total: float, url: str) -> None:
        if self.log is not None:
            self.log.write(time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           event=event, pause_seconds=round(pause, 1),
                           total_paused_seconds=round(total, 1),
                           interval_seconds=self.interval, url=url)


# The single limiter every module uses for arXiv (API and /src/ downloads).
ARXIV = RateLimiter(min_interval=3.0, cooldown=60.0, max_cooldown=300.0,
                    slow_interval=6.0, recover_after=30, name="arXiv")


def _report_to_arxiv_limiter(response, *args, **kwargs):
    """requests response hook: tell ARXIV how arXiv answered. Answers from
    other servers (Semantic Scholar has its own limiter) are ignored."""
    host = urlsplit(response.url).hostname or ""
    if any(host == h or host.endswith("." + h) for h in ARXIV_HOSTS):
        if response.status_code == 429:
            ARXIV.throttled(_retry_after_seconds(response), response.url)
        elif response.status_code < 400:
            ARXIV.succeeded()
    return None  # keep the response unchanged


def make_session(user_agent: str = DEFAULT_USER_AGENT) -> requests.Session:
    """HTTP session that identifies the crawler, as arXiv asks, and reports
    arXiv's answers to the shared ARXIV limiter."""
    session = requests.Session()
    session.headers["User-Agent"] = user_agent
    session.hooks["response"].append(_report_to_arxiv_limiter)
    return session


def get_with_retry(session: requests.Session, url: str, *, params=None,
                   limiter: RateLimiter = ARXIV, timeout: float = 60,
                   max_attempts: int = 5, first_backoff: float = 3.0):
    """GET `url` through `limiter`, retrying transient failures.

    Network errors and HTTP 429/5xx are retried after 3, 6, 12, 24 seconds
    (or the server's Retry-After when it is longer; after a 429 the limiter's
    cool-down also applies). The whole response body is read while the
    limiter is held, so only one connection is ever open.

    Returns (response, attempts, error): `response` is the last response
    received (None after a network error) and `error` is "" on HTTP 200.
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
