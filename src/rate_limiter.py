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
    for 1 s, i.e. 2 requests per second on average, with up to 8 downloads
    open at once (ARXIV_CONNECTIONS in the environment changes it, at most 10).
    A source file takes about 5.5 s to arrive (a 5200-ID crawl of October 2026:
    8376 requests in 11516 s outside the pauses, 4 at a time), so 4 or 6 open
    downloads cannot keep this pace; the page sets no connection limit, and
    more connections do not start more requests per second.

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
    arXiv request pauses: 5 s, doubled each time arXiv refuses again after a
    pause, up to 2 minutes, or longer if the server sends Retry-After. Several
    downloads are open at once, so one refusal often comes back on several of
    them: refusals that arrive during a pause belong to the same episode and
    do not double it again. The pace is not slowed afterwards, since the cause
    is the total load, not our rate.
  * After a pause only one request goes out (the probe). The others wait for
    its answer, so a refusal that goes on costs one request per pause and does
    not count against the files that happen to be next; a normal answer lets
    them all go. Every normal answer also halves the next pause: arXiv often
    refuses only part of the requests for an hour or more (a 5200-ID crawl of
    October 2026: 92 minutes of 5-minute pauses, each after one refusal among
    requests that mostly succeeded; 46% of its run time was spent waiting), and
    a pause that only came back down after 30 successes in a row cost 5 minutes
    per refusal.
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

Routes (Colab's own network, WARP, the Webshare proxies, AWS API Gateway;
see ProxyPool and "AWS" below):
when a refusal above would pause every request, and the request went
through a route while another route that serves the same kind of request
(source files: Colab, WARP and AWS, plus the Webshare proxies for a file
AWS found over 10 MB, see "Webshare"; API calls: every route) is not paused, only
that route pauses instead: 15 s, doubled at each new refusal of that route
up to 5 minutes, back to 15 s after it serves a request. ProxyPool skips a
paused route, so the other routes go on at the same shared pace (the routes
never add up to more than the pace above). The refusal that leaves no other
route pauses every request as above. Without WARP or proxies there is one
route, and every refusal pauses every request, as before. Route pauses are
logged as "route_pause" in arxiv_throttle_log.csv; they do not count in
total_paused_seconds, since requests went on through the other routes.

AWS (requests-ip-rotator, only when AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
are set): API calls, and with ARXIV_AWS_FILES=1 (the default) source files
too, go first through the API Gateways of the account, in the regions listed in
ARXIV_AWS_REGIONS (comma-separated; empty: the library's default list, which
fails on an account whose organization allows only some regions); every request still
waits for its turn in the limiters above, so AWS adds a route, not speed.
API Gateway (REST) buffers an answer and refuses one over 10 MB, so a source
file over 10 MB, or one AWS fails to carry for any other reason (integration
timeout after 29 s, gateway deleted, no answer), is sent again at once
through Colab/WARP after waiting for a new turn of the pace, and that URL
never goes through AWS again in this process ("aws_fallback" in
arxiv_throttle_log.csv). API Gateway also turns a binary body into UTF-8 text
unless the API lists it as binary, so every gateway gets the binary media type
"*/*" when the crawler starts (once; then the stage is redeployed). Answers
of arXiv itself through AWS (200, 404, 429, 503...) are handled like any
other route; if arXiv answers HTTP 403 through AWS while Colab/WARP get the
file, three times, AWS is turned off for the rest of the process.

Webshare (several accounts, e.g. 5 free accounts x 10 IPs, all listed in
ARXIV_PROXIES / proxies.txt): the proxies of one account share its
user/password (the backbone form adds -1, -2... to the user), which is how
they are grouped into accounts. API calls use every proxy as before. With
ARXIV_PROXY_LARGE_FILES=1, a source file AWS refused as over 10 MB is sent
again through a two-level rotation instead of Colab/WARP alone: Colab,
WARP, then each account as ONE slot (and its IPs in turn inside it), so
every account carries the same share as Colab or WARP and spends its
bandwidth (1 GB a month on the free plan) evenly. The shared pace above is
unchanged: more IPs add routes, not speed. Webshare tells why it refused in
its answer to CONNECT: 402 (bandwidth used up, plan unpaid) drops every IP
of the account for the rest of the process; 407 (credentials refused, IP
removed from the list) drops that IP, and the account once 3 of its IPs
are dropped; any other proxy failure quarantines the IP for 5 minutes, and
the whole account when 3 of its IPs are quarantined at once. Files under
10 MB, and every file when AWS is off, keep going through Colab/WARP.

arXiv never says how long it will keep refusing. The pauses are therefore not
a guess of when the refusal ends, but how often one request checks whether it
has ended: about 5, 15, 35, 75, 155 and 315 seconds after the first refusal,
then every 2 minutes. If arXiv has refused or not answered every
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
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

# requests_ip_rotator (AWS) is imported only when AWS keys are set: see _get_api_gateway().
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

# AWS API Gateway (requests-ip-rotator): see "AWS" in the module docstring.
AWS_ROUTE = "aws"                      # route name (printed as "AWS")
AWS_SITE = "https://export.arxiv.org"  # the gateways forward to this site only
AWS_HOST = "export.arxiv.org"
AWS_BINARY_TYPE = "*/*"                # binary media type every gateway needs (see _make_binary)
# Source files through AWS too (those over 10 MB fall back to Colab/WARP): ARXIV_AWS_FILES=0 turns it off.
AWS_FILES = os.environ.get("ARXIV_AWS_FILES", "1").strip().lower() not in ("0", "false", "no", "off")
AWS_MAX_STRIKES = 5                    # failures of AWS itself in a row before it is skipped for 5 min
# Regions whose API Gateway may be used, e.g. "ap-southeast-2" (an account of a school's AWS
# Organization may be denied every other region by a service control policy). Empty: the default list.
AWS_REGIONS = [r.strip() for r in os.environ.get("ARXIV_AWS_REGIONS", "").split(",") if r.strip()]

# Webshare nhiều tài khoản (xem "Webshare" ở docstring). ARXIV_PROXY_LARGE_FILES=1: file nguồn AWS
# không chở được vì quá 10 MB đi xoay vòng Colab + WARP + từng tài khoản Webshare. Mặc định tắt: như cũ.
PROXY_LARGE_FILES = os.environ.get("ARXIV_PROXY_LARGE_FILES", "0").strip().lower() in ("1", "true", "yes", "on")
ACCOUNT_BAD_IPS = 3                    # số IP của một tài khoản cùng bị bỏ thì bỏ cả tài khoản
_TUNNEL_STATUS = re.compile(r"Tunnel connection failed: (\d{3})")  # mã proxy trả cho CONNECT (https)


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
        # max: không rút ngắn một IP/tài khoản đã bị bỏ hẳn (hết dung lượng, 407)
        _bad_until[proxy] = max(_bad_until.get(proxy, 0.0), now + QUARANTINE_SECONDS)
    if new:
        print(f"[proxy] {_route_name(proxy)}: {reason}, bỏ qua {QUARANTINE_SECONDS / 60:g} phút "
              f"(request được thử lại qua đường kế tiếp; không tính là arXiv từ chối)", flush=True)


_FOREVER = float("inf")  # bỏ hẳn cho phần còn lại của tiến trình (một bước của crawler)
_accounts_off: set[int] = set()  # tài khoản Webshare đã bỏ hẳn


def _proxy_failed(proxy: str, exc: BaseException) -> None:
    """Proxy `proxy` của vòng xoay không chở được request (lỗi kết nối). Với
    https, Webshare trả lý do trong câu trả lời CONNECT, requests đưa vào
    thông báo lỗi: "Tunnel connection failed: 402 Payment Required"."""
    m = _TUNNEL_STATUS.search(str(exc))
    status = int(m[1]) if m else 0
    _proxy_refused(proxy, status, f"lỗi kết nối ({type(exc).__name__}"
                   + (f", proxy trả HTTP {status})" if status else ")"))


def _proxy_refused(proxy: str, status: int, reason: str) -> None:
    """402: tài khoản hết dung lượng/chưa trả tiền -> bỏ cả tài khoản. 407:
    IP này không nhận user/pass (bị thay khỏi danh sách, sai mật khẩu) -> bỏ
    hẳn IP. Lỗi khác: tạm bỏ IP 5 phút như cũ. ACCOUNT_BAD_IPS IP của cùng một
    tài khoản cùng bị bỏ: lỗi nằm ở tài khoản, bỏ cả tài khoản (hẳn nếu là
    407, 5 phút nếu không) thay vì thử lần lượt từng IP còn lại."""
    n = _ACCOUNT_OF.get(proxy)
    if status == 402 and n:
        _drop_account(n, "hết dung lượng hoặc gói chưa trả (proxy trả HTTP 402)", _FOREVER)
        return
    if status == 407:
        with _bad_lock:
            new = _bad_until.get(proxy, 0.0) != _FOREVER
            _bad_until[proxy] = _FOREVER
        if new:
            print(f"[proxy] {_route_name(proxy)}: proxy trả HTTP 407 (không nhận user/pass, hoặc IP đã bị "
                  f"thay khỏi danh sách Webshare): bỏ IP này cho phần còn lại của bước", flush=True)
    else:
        _quarantine(proxy, reason)
    if not n:
        return
    ips = WEBSHARE_ACCOUNTS[n - 1]
    now = time.monotonic()
    with _bad_lock:
        bad = [p for p in ips if _bad_until.get(p, 0.0) > now]
        dead = [p for p in bad if _bad_until[p] == _FOREVER]
    if len(ips) > 1 and len(bad) >= min(ACCOUNT_BAD_IPS, len(ips)):
        if len(dead) >= min(ACCOUNT_BAD_IPS, len(ips)):
            _drop_account(n, f"{len(dead)} IP trả HTTP 407 (user/pass sai?)", _FOREVER)
        elif len(bad) < len(ips):
            _drop_account(n, f"{len(bad)} IP cùng lỗi kết nối", QUARANTINE_SECONDS)


def _drop_account(n: int, reason: str, seconds: float) -> None:
    """Bỏ mọi IP của tài khoản Webshare thứ `n` trong `seconds` giây (_FOREVER:
    cho phần còn lại của bước; bước sau tự thử lại một lần)."""
    ips = WEBSHARE_ACCOUNTS[n - 1]
    with _bad_lock:
        if n in _accounts_off:
            return
        until = time.monotonic() + seconds
        for p in ips:
            _bad_until[p] = max(_bad_until.get(p, 0.0), until)
        if seconds == _FOREVER:
            _accounts_off.add(n)
        left = len(WEBSHARE_ACCOUNTS) - len(_accounts_off)
    how = "cho phần còn lại của bước" if seconds == _FOREVER else f"{seconds / 60:g} phút"
    print(f"[proxy] Webshare tk{n}: {reason}: bỏ cả {len(ips)} IP của tài khoản này {how} "
          f"(request đi đường khác; không tính là arXiv từ chối)", flush=True)
    if seconds == _FOREVER and not left and PROXY_LARGE_FILES:
        print("[proxy] Mọi tài khoản Webshare đều đã bỏ: file lớn đi Colab/WARP như trước", flush=True)


# Routes refused by arXiv (HTTP 429, or 503 on several files): skipped by ProxyPool until
# then, while the other routes go on (see "Routes" above). '' is Colab's own network.
ROUTE_COOLDOWN, ROUTE_MAX_COOLDOWN = 15.0, 300.0
_route_paused_until: dict[str, float] = {}
_route_next_cooldown: dict[str, float] = {}


def _route_name(route: str) -> str:
    if not route:
        return "Colab"
    if route == AWS_ROUTE:
        return "AWS"
    if route.endswith(f"{WARP_HOST}:{WARP_PORT}"):
        return "WARP"
    if route in _ACCOUNT_OF:
        return f"Webshare tk{_ACCOUNT_OF[route]} {_mask(route)}"
    return _mask(route)


def _routes_for(url: str) -> list[str]:
    """Routes that can carry `url`: Colab/WARP for source files (plus the
    Webshare proxies for a file over 10 MB, see _is_large), every proxy for
    API calls, plus AWS when it can carry this URL."""
    if _is_source(url):
        routes = list(_LARGE_FILE_LIST if _is_large(url) else _UNLIMITED_LIST)
    else:
        routes = list(PROXIES)
    if _aws_serves(url):
        routes.append(AWS_ROUTE)
    return routes


def _pause_route(route: str, url: str, retry_after: float) -> float | None:
    """Pause `route` alone when another route that serves `url` is not
    paused; returns the pause in seconds (0 when it was already paused), or
    None when no route would be left (the caller then pauses every request)."""
    routes = _routes_for(url)
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


_large_urls: set[str] = set()  # file AWS trả "quá 10 MB" trong tiến trình này


def _is_large(url: str) -> bool:
    """File lớn đi vòng xoay có Webshare: chỉ khi bật ARXIV_PROXY_LARGE_FILES,
    có proxy, và AWS đã báo file này quá 10 MB (AWS là nơi biết cỡ file trước
    khi tải: không có AWS thì mọi file đi Colab/WARP như cũ)."""
    return PROXY_LARGE_FILES and url in _large_urls and len(_LARGE_FILE_LIST) > len(_UNLIMITED_LIST)


# ---- AWS route state (one crawler process) ----
_aws_lock = threading.Lock()
_aws_skip: set[str] = set()  # URLs AWS could not carry (over 10 MB...): other routes from now on
_aws_strikes = 0             # failures of AWS itself in a row (not answers of arXiv)
_aws_fallbacks = 0           # requests sent again through another route
_aws_blocked = 0             # arXiv answered 403 through AWS while another route got the file
_aws_off = ""                # why AWS was turned off for the rest of this process


def _aws_serves(url: str) -> bool:
    """AWS can carry `url` (pauses aside): gateways running, export.arxiv.org,
    a source file only with ARXIV_AWS_FILES, and not a URL AWS failed before."""
    if not _gateway or _aws_off:
        return False
    if (urlsplit(url).hostname or "") != AWS_HOST:
        return False
    if _is_source(url) and not AWS_FILES:
        return False
    return url not in _aws_skip


def _aws_usable(url: str) -> bool:
    """_aws_serves() and the AWS route is neither paused (arXiv refused it)
    nor quarantined (AWS itself kept failing)."""
    if not _aws_serves(url):
        return False
    now = time.monotonic()
    return _route_paused_until.get(AWS_ROUTE, 0.0) <= now and _bad_until.get(AWS_ROUTE, 0.0) <= now


def _aws_fallback(url: str, reason: str, own_failure: bool) -> None:
    """AWS could not carry `url`: it goes through Colab/WARP (or a proxy) from
    now on. `own_failure`: AWS itself failed (not the 10 MB limit, not an answer
    of arXiv); AWS_MAX_STRIKES of them in a row skip AWS for 5 minutes."""
    global _aws_strikes, _aws_fallbacks
    _aws_skip.add(url)
    with _aws_lock:
        _aws_fallbacks += 1
        count = _aws_fallbacks
        _aws_strikes = _aws_strikes + 1 if own_failure else 0
        quarantine = _aws_strikes >= AWS_MAX_STRIKES
        if quarantine:
            _aws_strikes = 0
    if count <= 3 or count % 50 == 0:
        other = ("Colab/WARP/Webshare" if _is_large(url) else "Colab/WARP") if _is_source(url) else "đường khác"
        print(f"[AWS] {urlsplit(url).path}: {reason}; gửi lại qua {other}"
              + (f" (lần thứ {count} trong bước này)" if count > 3 else ""), flush=True)
    ARXIV_EXPORT._log("aws_fallback", reason, 0, ARXIV_EXPORT.paused_seconds, url)
    if quarantine:
        _quarantine(AWS_ROUTE, f"{AWS_MAX_STRIKES} lỗi của chính AWS liên tiếp")


def _aws_answered() -> None:
    """arXiv answered through AWS: AWS itself works."""
    global _aws_strikes
    if _aws_strikes:
        with _aws_lock:
            _aws_strikes = 0


def _aws_refused_by_arxiv() -> None:
    """arXiv answered 403 through AWS but served the same URL through another
    route: after three times AWS is turned off for the rest of this process."""
    global _aws_blocked, _aws_off
    with _aws_lock:
        _aws_blocked += 1
        turn_off = _aws_blocked >= 3 and not _aws_off
        if turn_off:
            _aws_off = "arXiv trả HTTP 403 cho AWS nhưng vẫn cho Colab/WARP tải"
    if turn_off:
        print(f"[AWS] {_aws_off} (3 lần): tắt AWS cho phần còn lại của bước này", flush=True)


def _gateway_error(response) -> str:
    """'' when `response` is arXiv's own answer passed through by API Gateway;
    otherwise a short description of the error API Gateway made itself (body
    over 10 MB, integration timeout after 29 s, throttling, deleted gateway...).
    Those carry x-amzn-ErrorType and/or a JSON {"message": ...} body; arXiv
    never answers JSON."""
    kind = response.headers.get("x-amzn-ErrorType", "").split(":")[0]
    json_body = response.headers.get("Content-Type", "").lower().startswith("application/json")
    if not kind and not (response.status_code >= 400 and json_body):
        return ""
    message = ""
    if json_body:
        try:  # a few bytes: reading them does not matter, the response is dropped
            message = str(response.json().get("message", ""))[:120]
        except Exception:
            pass
    return " ".join(p for p in (f"AWS trả HTTP {response.status_code}", kind, message) if p)


def _wait_new_turn(url: str) -> None:
    """The request AWS could not carry may have reached arXiv, so the request
    sent again through another route waits for a new turn of the pace first
    (the caller already holds a connection of the limiter: none is taken)."""
    if not _is_source(url):
        ARXIV._wait_turn()
    ARXIV_EXPORT._wait_turn()


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
            # mọi đường đang nghỉ: đường dùng lại được sớm nhất (cùng lúc thì mạng gốc)
            return min(self._proxies, key=_free_again_at)

    def next_proxies_dict(self) -> dict[str, str] | None:
        """Trả về dict proxies chuẩn cho thư viện requests (chuỗi '' = dùng mạng gốc)."""
        proxy = self.next()
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    def __bool__(self) -> bool:
        return bool(self._proxies)


def _free_again_at(proxy: str) -> tuple[float, bool]:
    """Khi nào `proxy` dùng lại được: hết tạm dừng (arXiv từ chối) và hết bị
    bỏ (lỗi proxy, tài khoản hết dung lượng); mạng gốc không bao giờ bị bỏ."""
    bad = _bad_until.get(proxy, 0.0) if proxy else 0.0
    return max(_route_paused_until.get(proxy, 0.0), bad), bool(proxy)


class AccountPool:
    """Xoay vòng hai tầng cho file lớn: lần lượt từng nhóm (Colab, WARP, rồi
    MỖI TÀI KHOẢN Webshare là một nhóm), trong nhóm thì lần lượt từng IP.
    Nhờ vậy mỗi tài khoản nhận phần bằng Colab hay WARP (5 tài khoản: mỗi
    đường 1/7 số file lớn), dung lượng của từng tài khoản dùng đều, và mạng
    Colab/WARP (không giới hạn dung lượng) vẫn gánh phần của mình. Bỏ qua
    đường đang tạm dừng và IP/tài khoản đang bị bỏ như ProxyPool."""

    def __init__(self, groups: list[list[str]]):
        self._groups = [list(g) for g in groups if g]
        self._all = [p for g in self._groups for p in g]
        self._g = 0
        self._i = [0] * len(self._groups)
        self._lock = threading.Lock()

    def next(self) -> str | None:
        if not self._all:
            return None
        with self._lock:
            now = time.monotonic()
            for _ in range(len(self._groups)):
                g = self._g % len(self._groups)
                self._g += 1
                group = self._groups[g]
                for _ in range(len(group)):
                    proxy = group[self._i[g] % len(group)]
                    self._i[g] += 1
                    if _route_paused_until.get(proxy, 0.0) > now:
                        continue
                    if not proxy or _bad_until.get(proxy, 0.0) <= now:
                        return proxy
            return min(self._all, key=_free_again_at)

    def __bool__(self) -> bool:
        return bool(self._all)


def _group_accounts(proxies: list[str]) -> list[list[str]]:
    """Gom proxy theo tài khoản Webshare: các IP của một tài khoản dùng chung
    user/pass (dạng backbone p.webshare.io thêm -1, -2... vào user). Proxy
    không có user/pass thì gom theo máy chủ. Giữ thứ tự, bỏ dòng trùng."""
    accounts: dict[str, list[str]] = {}
    for p in dict.fromkeys(proxies):
        parts = urlsplit(p)
        if parts.username:
            key = re.sub(r"-(\d+|rotate)$", "", parts.username) + ":" + (parts.password or "")
        else:
            key = parts.hostname or p
        accounts.setdefault(key, []).append(p)
    return list(accounts.values())


# 1. Danh sách không giới hạn dung lượng (Mạng gốc + WARP nếu máy có bật) -> Dùng tải /src/, /pdf/
_UNLIMITED_LIST: list[str] = [""]
if _is_warp_running():
    if _socks_available():
        _UNLIMITED_LIST.append(f"socks5h://{WARP_HOST}:{WARP_PORT}")
    else:
        print(f'[proxy] WARP đang mở ở {WARP_HOST}:{WARP_PORT} nhưng thiếu PySocks '
              f'(pip install "requests[socks]"): bỏ qua WARP', flush=True)

# 2. Danh sách cho API nhẹ -> Gồm _UNLIMITED_LIST + mọi IP Webshare (ARXIV_PROXIES / proxies.txt)
_EXTERNAL_PROXIES: list[str] = _load_external_proxies()
PROXIES: list[str] = _UNLIMITED_LIST + _EXTERNAL_PROXIES

PROXY_POOL = ProxyPool(PROXIES)
UNLIMITED_POOL = ProxyPool(_UNLIMITED_LIST)

# 3. File > 10 MB (AWS không chở được), khi bật ARXIV_PROXY_LARGE_FILES: Colab + WARP + từng tài khoản
#    Webshare (5 tài khoản x 10 IP -> 7 nhóm xoay vòng, xem AccountPool).
WEBSHARE_ACCOUNTS: list[list[str]] = _group_accounts(_EXTERNAL_PROXIES)
_ACCOUNT_OF: dict[str, int] = {p: n for n, ips in enumerate(WEBSHARE_ACCOUNTS, 1) for p in ips}
_LARGE_FILE_LIST: list[str] = _UNLIMITED_LIST + [p for ips in WEBSHARE_ACCOUNTS for p in ips]
LARGE_FILE_POOL = AccountPool([[r] for r in _UNLIMITED_LIST] + WEBSHARE_ACCOUNTS)


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
    `isolation_window` seconds; succeeded() ends a run of refusals, halves the
    next cool-down and brings the normal pace back after `recover_after`
    successful requests in a row. After a pause only one request starts until
    it is answered (see the module docstring).
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
        self._probe_needed = False  # after a pause: one request first, the others wait for its answer
        self._probe_thread = None   # the thread whose request is the probe

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
        if self._probe_thread == threading.get_ident():
            with self._state:  # no answer that settled it (an error...): the next request is the probe
                self._probe_thread = None
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
                if start_at <= now and self._probe_needed:
                    if self._probe_thread is None:
                        self._probe_thread = threading.get_ident()
                    else:
                        start_at = now + 0.05  # the probe is out: wait for its answer
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
                self._probe_needed, self._probe_thread = True, None
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
        no longer suspect). Each answer after a pause halves the next cool-down;
        after `recover_after` successes in a row the normal pace comes back."""
        if not self.cooldown:
            return
        with self._state:
            self._refused_since = None  # any normal answer ends the run of refusals
            self._last_failure = None   # and a later single failure is isolated again
            if url:
                self._file_failures.pop(url, None)
                self._suspects.discard(url)
            if self._probe_thread == threading.get_ident():
                self._probe_needed, self._probe_thread = False, None  # arXiv answers: all may go
            came_back = False
            if time.monotonic() >= self._paused_until:  # not a request sent before the pause
                came_back = self._next_cooldown > self.cooldown >= self._next_cooldown / 2
                self._next_cooldown = max(self._next_cooldown / 2, self.cooldown)
            if self.interval == self.min_interval:
                total = self.paused_seconds
                if came_back:
                    print(f"[{self.name}] answering again: the next pause would be {self.cooldown:g} s", flush=True)
                    self._log("recover", "", 0, total, "")
                return  # the spacing is already normal
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
def _export_connections() -> int:
    """Downloads open at once: ARXIV_CONNECTIONS, 8 by default, at most 10 (the
    size of the connection pool of a requests session)."""
    value = os.environ.get("ARXIV_CONNECTIONS", "")
    return min(max(int(value), 1), 10) if value.isdigit() else 8


ARXIV_EXPORT = RateLimiter(min_interval=0.25, burst=4, burst_pause=1.0, connections=_export_connections(),
                           cooldown=5.0, max_cooldown=120.0, slow_interval=6.0,
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
    """Transport adapter: chooses the route of every arXiv request (AWS first
    when it can carry the URL, otherwise the Colab/WARP rotation for source
    files and the proxy rotation for API calls) and reports failed connections
    to ARXIV_EXPORT. Other hosts (Semantic Scholar...) go straight out."""

    def __init__(self, proxy_pool: ProxyPool | None = PROXY_POOL,
                 gateway_adapter: HTTPAdapter | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_pool = proxy_pool
        self.gateway_adapter = gateway_adapter  # requests_ip_rotator.ApiGateway, or None

    def send(self, request, *args, **kwargs):
        if not _is_arxiv(request.url):
            return super().send(request, *args, **kwargs)
        url = request.url
        explicit = kwargs.get("proxies")
        if _has_explicit_proxy(explicit):  # the caller chose the proxy: keep it
            route = explicit.get("https") or explicit.get("http") or ""
            return self._send_route(request, route, "", args, kwargs)

        aws_403 = False
        if self.gateway_adapter is not None and _aws_usable(url):
            response, aws_403 = self._send_aws(request, args, kwargs)
            if response is not None:
                return response
            _wait_new_turn(url)  # the same request again, through another route

        if _is_source(url):  # file > 10 MB (AWS vừa báo): vòng xoay có Webshare; còn lại Colab/WARP như cũ
            pool = LARGE_FILE_POOL if _is_large(url) else UNLIMITED_POOL
        else:
            pool = self.proxy_pool
        chosen = (pool.next() if pool else None) or ""
        if chosen:
            kwargs["proxies"] = {"http": chosen, "https": chosen}
        response = self._send_route(request, chosen, chosen, args, kwargs)
        if aws_403 and response.status_code < 400:
            _aws_refused_by_arxiv()
        return response

    def _send_route(self, request, route: str, chosen: str, args, kwargs):
        """Send through Colab's network or the proxy in kwargs (`chosen`: a
        proxy of the rotation, quarantined when it cannot connect)."""
        try:
            response = super().send(request, *args, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if chosen:
                _proxy_failed(chosen, exc)  # 402/407 của Webshare: bỏ cả tài khoản / IP; còn lại: 5 phút
            else:
                ARXIV_EXPORT.failed(request.url, f"no answer ({type(exc).__name__})", route=route)
            raise
        if chosen in _ACCOUNT_OF and response.status_code in (402, 407):
            # URL http:// qua proxy: Webshare trả lỗi như một câu trả lời (arXiv không bao giờ trả 402/407).
            response.close()
            _proxy_refused(chosen, response.status_code, f"proxy trả HTTP {response.status_code}")
            raise requests.exceptions.ProxyError(f"proxy trả HTTP {response.status_code}", request=request)
        response.arxiv_route = route
        return response

    def _send_aws(self, request, args, kwargs):
        """Send through AWS API Gateway. Returns (response, False) when arXiv
        answered through AWS, else (None, arxiv_403) and the caller sends the
        request through another route. Failures of AWS itself are not arXiv's
        and are never reported to ARXIV_EXPORT."""
        url = request.url
        try:
            # ApiGateway.send() rewrites the URL and headers it is given: give it a copy.
            response = self.gateway_adapter.send(request.copy(), *args, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            _aws_fallback(url, f"AWS không trả lời ({type(exc).__name__})", own_failure=True)
            return None, False
        error = _gateway_error(response)
        if error:
            response.close()
            low = error.lower()
            too_large = response.status_code == 413 or "large" in low or "size" in low
            if too_large:
                _large_urls.add(url)  # gửi lại qua vòng xoay file lớn (Colab/WARP + Webshare nếu bật)
            _aws_fallback(url, error + (" (quá 10 MB)" if too_large else ""), own_failure=not too_large)
            return None, False
        _aws_answered()
        if response.status_code == 403:  # arXiv refuses AWS's address? Ask through Colab/WARP.
            response.close()
            _aws_fallback(url, "arXiv trả HTTP 403 qua AWS", own_failure=False)
            return None, True
        # Callers, redirects and the response hook see arXiv's URL, not the gateway's.
        response.url = url
        response.request = request
        response.arxiv_route = AWS_ROUTE
        return response, False


_gateway = None  # None: not started yet; False: no AWS in this process; else the ApiGateway
_gateway_lock = threading.Lock()


def _get_api_gateway():
    """The AWS API Gateways of this process (started once), or None without
    AWS keys or when they cannot be used (requests then use the other routes)."""
    global _gateway
    if _gateway is None:
        with _gateway_lock:
            if _gateway is None:
                _gateway = _start_api_gateway() or False
    return _gateway or None


def _start_api_gateway():
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (access_key and secret_key):
        return None
    try:
        from requests_ip_rotator import ApiGateway
        regions = {"regions": AWS_REGIONS} if AWS_REGIONS else {}
        gateway = ApiGateway(AWS_SITE, access_key_id=access_key,
                             access_key_secret=secret_key, verbose=False, **regions)
        # start(): in every region, reuses the gateway named after the site, or creates one.
        if not gateway.start():
            raise RuntimeError("không vùng nào có gateway (key thiếu quyền API Gateway, hoặc vùng bị chặn?)")
        gateway.endpoints = _make_binary(gateway.endpoints, access_key, secret_key)
        if not gateway.endpoints:
            raise RuntimeError(f"không gateway nào bật được kiểu nhị phân {AWS_BINARY_TYPE}")
    except Exception as exc:
        print(f"[proxy] Lỗi khởi động AWS API Gateway: {type(exc).__name__}: {str(exc)[:200]}. "
              f"Sẽ dùng ProxyPool thay thế.", flush=True)
        return None
    what = "API arXiv và file nguồn < 10 MB" if AWS_FILES else "API arXiv (file nguồn: tắt)"
    print(f"[proxy] Đã khởi động AWS API Gateway thành công: {len(gateway.endpoints)} vùng"
          + (f" ({', '.join(AWS_REGIONS)})" if AWS_REGIONS else "") + ", "
          f"{what} đi qua AWS.", flush=True)
    return gateway


def _make_binary(endpoints: list[str], access_key: str, secret_key: str) -> list[str]:
    """API Gateway (REST) passes a body through unchanged only when the API
    lists its media type as binary; otherwise a .tar.gz comes out as UTF-8
    text. Add "*/*" to every gateway that lacks it and redeploy its stage
    (requests-ip-rotator does not), then return the endpoints that are ready:
    a gateway that cannot be fixed is not used."""
    import boto3

    def fix(endpoint: str) -> bool:
        api_id, _, region = endpoint.split(".")[:3]  # <id>.execute-api.<region>.amazonaws.com
        client = boto3.session.Session().client(
            "apigateway", region_name=region,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        if AWS_BINARY_TYPE in (client.get_rest_api(restApiId=api_id).get("binaryMediaTypes") or []):
            return False
        path = "/binaryMediaTypes/" + AWS_BINARY_TYPE.replace("~", "~0").replace("/", "~1")
        client.update_rest_api(restApiId=api_id, patchOperations=[{"op": "add", "path": path}])
        client.create_deployment(restApiId=api_id, stageName="ProxyStage",
                                 description=f"binary media type {AWS_BINARY_TYPE}")
        return True

    ready, changed, failed = [], 0, []
    with ThreadPoolExecutor(max_workers=10) as pool:
        jobs = [(endpoint, pool.submit(fix, endpoint)) for endpoint in endpoints]
        for endpoint, job in jobs:
            try:
                changed += job.result()
                ready.append(endpoint)
            except Exception as exc:
                failed.append(f"{endpoint.split('.')[2]}: {type(exc).__name__}")
    if failed:
        print(f"[proxy] AWS: bỏ {len(failed)} gateway không bật được kiểu nhị phân "
              f"({'; '.join(failed)[:300]})", flush=True)
    if changed:
        print(f"[proxy] AWS: đã bật kiểu nhị phân {AWS_BINARY_TYPE} cho {changed} gateway; "
              f"chờ 15 s cho cấu hình mới có hiệu lực", flush=True)
        time.sleep(15)
    return ready


def make_session(user_agent: str = DEFAULT_USER_AGENT,
                 proxy_pool: ProxyPool | None = PROXY_POOL) -> requests.Session:
    """HTTP session that identifies the crawler, as arXiv asks, and reports
    arXiv's answers and failed connections to the shared ARXIV_EXPORT limiter.
    With AWS keys in the environment, arXiv requests go through AWS first."""
    session = requests.Session()
    session.headers["User-Agent"] = user_agent
    session.hooks["response"].append(_report_to_arxiv_limiter)
    adapter = _ArxivAdapter(proxy_pool=proxy_pool, gateway_adapter=_get_api_gateway())
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    _announce_large_files()
    return session


_announced = False


def _announce_large_files() -> None:
    """In một lần mỗi tiến trình: file > 10 MB đi những đường nào."""
    global _announced
    if _announced or not PROXY_LARGE_FILES:
        return
    _announced = True
    if not WEBSHARE_ACCOUNTS:
        print("[proxy] ARXIV_PROXY_LARGE_FILES bật nhưng không có proxy nào: file lớn đi Colab/WARP", flush=True)
    elif not _gateway or not AWS_FILES:
        print("[proxy] ARXIV_PROXY_LARGE_FILES bật nhưng file nguồn không qua AWS (cần DUNG_AWS + AWS_TAI_FILE): "
              "không biết trước file nào > 10 MB, mọi file đi Colab/WARP như cũ", flush=True)
    else:
        sizes = " + ".join(str(len(ips)) for ips in WEBSHARE_ACCOUNTS)
        names = ", ".join(_route_name(r) for r in _UNLIMITED_LIST)
        print(f"[proxy] File > 10 MB: xoay vòng {names} và {len(WEBSHARE_ACCOUNTS)} tài khoản Webshare "
              f"({sizes} IP), mỗi tài khoản một lượt như Colab/WARP; nhịp chung với arXiv không đổi", flush=True)


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
