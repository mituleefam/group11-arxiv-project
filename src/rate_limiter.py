"""Shared, polite access to arXiv.

arXiv asks for at least 3 seconds between requests and only one connection at
a time. The limit applies to the whole program, so every module that calls
arXiv (discovery, metadata, source download, reference dates) must go through
the same `ARXIV` limiter instead of sleeping on its own.

arXiv may still answer HTTP 429 ("too many requests") or HTTP 503 ("service
unavailable"). arXiv staff explain that "Rate exceeded" (429) "relates to
server capacity and total number of users, not to individual usage", that a
503 is what excessive use gets, and that "the only fix is try again later"
(arXiv API group, 2025-11-17, https://groups.google.com/a/arxiv.org/g/api/c/pNB3lnxf4mQ).
Using more machines or IP addresses to get around it is forbidden by the
Terms of Use (https://info.arxiv.org/help/api/tou.html).

Retrying one request a few seconds later is not enough: the next download
would start at once, be refused too, and a whole chain of versions would
fail. So on a 429 or 503 the limiter pauses *every* arXiv request for a
cool-down (60 s, doubled at each new refusal up to 5 minutes, longer if the
server sends Retry-After) and spaces requests 6 s apart until 30 requests in
a row have succeeded. A request that gets no answer at all (network down,
DNS failure, server silent) is handled the same way. When arXiv always
answers, nothing changes: requests stay 3 s apart.

arXiv never says how long it will keep refusing. The pauses are therefore not
a guess of when the refusal ends, but how often one single request checks
whether it has ended: about 1, 3, 7, 12, 17, 22 and 27 minutes after the first
refusal, then every 5 minutes. If arXiv has refused or not answered every
request for `give_up_after` seconds (30 minutes by default, set with
main.py --max-wait-minutes), ArxivUnavailable is raised: the crawl stops with
a clear message instead of waiting for hours, and everything finished so far
stays saved.

Sessions from make_session() report every arXiv answer and every failed
connection to `ARXIV` (a response hook and a transport adapter), so the
modules that send the requests need no change.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

DEFAULT_USER_AGENT = "NMKHDL-arxiv-crawler/1.0 (university course project)"

# Status codes worth retrying: rate limited or a temporary server problem.
RETRY_STATUS = {429, 500, 502, 503, 504}

# Answers from these hosts are reported to ARXIV (see make_session).
ARXIV_HOSTS = ("arxiv.org",)
# arXiv answers that make ARXIV pause: too many requests, server unavailable.
PAUSE_STATUS = {429, 503}

# Columns of logs/arxiv_throttle_log.csv: one row per pause or recovery, so the
# report can tell time lost to arXiv refusals from the crawler's own time.
THROTTLE_LOG_FIELDS = ["time", "event", "reason", "pause_seconds",
                       "total_paused_seconds", "interval_seconds", "url"]


class ArxivUnavailable(RuntimeError):
    """arXiv refused or did not answer every request for `give_up_after`
    seconds. It is not a requests error, so no module retries it: the crawl
    stops and main.py explains what to do."""


class RateLimiter:
    """Context manager that allows one request at a time and keeps request
    starts at least `min_interval` seconds apart.

        with ARXIV:
            response = session.get(url, timeout=60)

    The interval is counted from the start of the previous request: if a
    download already took 7 seconds, the next request starts immediately.

    With `cooldown` > 0 the limiter also reacts to HTTP 429/503: throttled()
    pauses every request for the cool-down and switches to `slow_interval`;
    succeeded() brings the normal pace back after `recover_after` successful
    requests in a row. With the default cooldown=0 both calls do nothing.
    With `give_up_after` > 0, entering the limiter raises ArxivUnavailable once
    every request has been refused for that many seconds.
    """

    def __init__(self, min_interval: float, *, cooldown: float = 0.0,
                 max_cooldown: float = 0.0, slow_interval: float | None = None,
                 recover_after: int = 30, give_up_after: float = 0.0,
                 name: str = "limiter"):
        self.min_interval = min_interval
        self.interval = min_interval  # current spacing, slower after a 429/503
        self.cooldown = cooldown
        self.max_cooldown = max(max_cooldown, cooldown)
        self.slow_interval = max(slow_interval or min_interval, min_interval)
        self.recover_after = recover_after
        self.give_up_after = give_up_after
        self.name = name
        self.log = None  # optional CsvLog with THROTTLE_LOG_FIELDS (set by main.py)
        self.throttle_count = 0      # HTTP 429/503 answers seen
        self.paused_seconds = 0.0    # total time all requests were held back
        self._lock = threading.Lock()   # one request at a time
        self._state = threading.Lock()  # pause bookkeeping, held only briefly
        self._last_start = float("-inf")
        self._paused_until = float("-inf")
        self._next_cooldown = cooldown
        self._successes = 0
        self._refused_since = None  # start of the current run of refusals
        self._gave_up = False

    def __enter__(self):
        self._lock.acquire()  # one connection at a time
        try:
            with self._state:
                start_at = max(self._last_start + self.interval, self._paused_until)
            wait = start_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)  # only wait for what is left of the interval or pause
            self._check_give_up()
        except BaseException:  # giving up, or Ctrl+C during a pause
            self._lock.release()
            raise
        self._last_start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False

    def throttled(self, retry_after: float = 0.0, url: str = "", reason: str = "HTTP 429") -> None:
        """The server refused a request (HTTP 429 or 503) or did not answer:
        hold back every request for the current cool-down (or Retry-After when
        longer), double the next cool-down and slow the pace until requests
        succeed again."""
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
            if self._refused_since is None:
                self._refused_since = now
            refused_for = now - self._refused_since
            total = self.paused_seconds
        so_far = f"{refused_for:.0f} s" if refused_for < 120 else f"{refused_for / 60:.0f} min"
        limit = (f"; the crawl stops if this lasts {self.give_up_after / 60:.0f} min"
                 if self.give_up_after else "")
        print(f"[{self.name}] {reason}: pausing all {self.name} requests for {pause:.0f} s "
              f"(refused for {so_far} so far{limit})", flush=True)
        self._log("pause", reason, pause, total, url)

    def succeeded(self) -> None:
        """The server answered normally. After `recover_after` successes in a
        row the normal pace and the first cool-down length come back."""
        if not self.cooldown:
            return
        with self._state:
            self._refused_since = None  # any normal answer ends the run of refusals
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
            f"longer. Everything finished so far is saved. arXiv says these refusals come from "
            f"the total load on its servers and the only fix is to try again later: check that "
            f"no other crawler of yours is running, then run the same command again later (for "
            f"example in 1-2 hours), or use --max-wait-minutes to wait longer next time.")

    def _log(self, event: str, reason: str, pause: float, total: float, url: str) -> None:
        if self.log is not None:
            self.log.write(time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           event=event, reason=reason, pause_seconds=round(pause, 1),
                           total_paused_seconds=round(total, 1),
                           interval_seconds=self.interval, url=url)


# The single limiter every module uses for arXiv (API and /src/ downloads).
ARXIV = RateLimiter(min_interval=3.0, cooldown=60.0, max_cooldown=300.0,
                    slow_interval=6.0, recover_after=30, give_up_after=1800.0, name="arXiv")


def _is_arxiv(url: str) -> bool:
    """True for arXiv URLs; other servers (Semantic Scholar has its own
    limiter and retries) are never reported to ARXIV."""
    host = urlsplit(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in ARXIV_HOSTS)


def _report_to_arxiv_limiter(response, *args, **kwargs):
    """requests response hook: tell ARXIV how arXiv answered."""
    if _is_arxiv(response.url):
        if response.status_code in PAUSE_STATUS:
            ARXIV.throttled(_retry_after_seconds(response), response.url,
                            f"HTTP {response.status_code}")
        elif response.status_code < 400:
            ARXIV.succeeded()
    return None  # keep the response unchanged


class _ArxivAdapter(HTTPAdapter):
    """Transport adapter that also reports to ARXIV when arXiv gives no answer
    at all (network down, DNS failure, no reply in time), so the following
    requests wait for the cool-down instead of failing one after the other.
    A connection cut while a body is being read is not reported: that error
    comes later, from reading the body, and downloader.py resumes the file."""

    def send(self, request, *args, **kwargs):
        try:
            return super().send(request, *args, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if _is_arxiv(request.url):
                ARXIV.throttled(0.0, request.url, f"no answer ({type(exc).__name__})")
            raise


def make_session(user_agent: str = DEFAULT_USER_AGENT) -> requests.Session:
    """HTTP session that identifies the crawler, as arXiv asks, and reports
    arXiv's answers and failed connections to the shared ARXIV limiter."""
    session = requests.Session()
    session.headers["User-Agent"] = user_agent
    session.hooks["response"].append(_report_to_arxiv_limiter)
    adapter = _ArxivAdapter()
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
