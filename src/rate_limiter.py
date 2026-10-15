"""Shared, polite access to arXiv.

arXiv asks for at least 3 seconds between requests and only one connection at
a time. The limit applies to the whole program, so every module that calls
arXiv (discovery, metadata, source download) must go through the same `ARXIV`
limiter instead of sleeping on its own.
"""

from __future__ import annotations

import threading
import time

import requests

DEFAULT_USER_AGENT = "NMKHDL-arxiv-crawler/1.0 (university course project)"

# Status codes worth retrying: rate limited or a temporary server problem.
RETRY_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Context manager that allows one request at a time and keeps request
    starts at least `min_interval` seconds apart.

        with ARXIV:
            response = session.get(url, timeout=60)

    The interval is counted from the start of the previous request: if a
    download already took 7 seconds, the next request starts immediately.
    """

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_start = float("-inf")

    def __enter__(self):
        self._lock.acquire()  # one connection at a time
        wait = self._last_start + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)  # only wait for what is left of the interval
        self._last_start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


# The single limiter every module uses for arXiv (API and /src/ downloads).
ARXIV = RateLimiter(min_interval=3.0)


def make_session(user_agent: str = DEFAULT_USER_AGENT) -> requests.Session:
    """HTTP session that identifies the crawler, as arXiv asks."""
    session = requests.Session()
    session.headers["User-Agent"] = user_agent
    return session


def get_with_retry(session: requests.Session, url: str, *, params=None,
                   limiter: RateLimiter = ARXIV, timeout: float = 60,
                   max_attempts: int = 5, first_backoff: float = 3.0):
    """GET `url` through `limiter`, retrying transient failures.

    Network errors and HTTP 429/5xx are retried after 3, 6, 12, 24 seconds
    (or the server's Retry-After when it is longer). The whole response body
    is read while the limiter is held, so only one connection is ever open.

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
