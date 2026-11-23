# Milestone 1: Quan's work (Member 1) and the reasons behind it

This document lets the group follow what Quan added to the Milestone 1 crawler:
what each part does, which technique it uses, and **why**: the statement (quoted verbatim),
the lecturer's answers in the Q&A sheet, the arXiv / Semantic Scholar documentation, or measurements
from the group's real run logs. Quotes of the lecturer's answers are translated from Vietnamese.

- Statement: <https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/crawling>
- The lecturer's Q&A: the sheet "[IntroDS] QA Replies - 2627-1" (tabs *Milestone 1* and *Milestone 3*)
- On GitHub, Quan's commits appear under `EugeneZ-netizen` or `24127226 - PHẠM TRẦN ANH QUÂN`.

---

## 1. Quick summary

| Commit | Date | Main content |
| :-- | :-- | :-- |
| `3d40029` | 15/10 | `common.py`, `rate_limiter.py`: data folder layout, manifest, CSV logs, the shared rate limiter |
| `734950e` | 15/10 | `discovery.py`: from the assigned ID range to the list of papers and their number of versions |
| `2b834d3` | 15/10 | `metadata.py`, `main.py`: the metadata of each version, a one-command pipeline |
| `614b86e` | 24/10 | HTTP 429: pause **every** arXiv request for a while instead of breaking a whole chain of versions |
| `2909759` | 25/10 | A clear time limit when arXiv refuses, an immediate stop on Stop, one crawler per data folder |
| `7b9e8a0` | 25/10 | Download again a version whose files were lost, stop for good when the disk is full |
| `2d9dbc5` | 26/10 | Retry errors in **passes**, stop everything only on a general error, `--no-arxiv-dates` |
| `562b051` | 26/10 | `ref_dates_kaggle.py` (the dates of cited papers taken from Kaggle), `group_benchmark.py` |
| `a1582b5` | 27/10 | Parallel downloads at the arXiv bulk-data pace, 429s that do not add up, optional proxies |
| `ca0fe44`, `ae69170`, `101cbb5` | 27–28/10 | Colab notebooks (trial, group, final) |
| `1930137` | 28/10 | References: several papers in one Semantic Scholar `/paper/batch` request |
| `1d8fa3b` | 29/10 | The group crawl in steps saved to Drive (`chunked_crawl.py`, `snapshot.py`), putting aside arXiv files not delivered |
| `ddeecb9` | 03/11 | A route through AWS API Gateway (and several Webshare accounts for large files), 8 download connections, shorter waits and 1 probe request after each arXiv refusal |

The initial source download (`downloader.py`, `extractor.py`, `checkpoint.py`) is
Member 2's, the initial references (`references.py`) Member 3's, and validation and benchmarking
(`validate.py`, `benchmark.py`, `corpus_statistics.py`) Member 4's. Quan changed
these files further in the commits listed in the table.

---

## 2. What the statement asks and which file meets it

| Statement (verbatim) | Where in the code |
| :-- | :-- |
| "resolve these assigned IDs and to locate every version of each paper" | `discovery.py` |
| "Each version is retrieved individually through the versioned /src/ endpoint" | `downloader.py` (`export.arxiv.org/src/<id>v<N>`) |
| "Point automated downloads at this mirror and never at the main site arxiv.org" | `rate_limiter.py`, `downloader.py`: only `export.arxiv.org` is called |
| "wait at least 3 seconds between successive calls and use one connection at a time" (API) | `rate_limiter.ARXIV`: 3 s, 1 connection |
| "for such versions you should keep an empty record in the corpus rather than dropping the publication" | `versions.json` records `pdf_only`, `no_tex`, `not_found` |
| "extract only the .tex and .bib files from each source archive" | `extractor.py` (Member 2); `.bbl` files are also kept since the Q&A answer (see 4.3) |
| "The reference metadata must additionally be crawled for each publication via the Semantic Scholar API" | `references.py` (Member 3), Quan added `/paper/batch` |
| "Keep one working folder per publication, for example folder 2310-12345" | `common.DataLayout`: `corpus/<yymm-id>/` |
| "running time and memory footprint ... the maximum disk storage required" | `benchmark.py` (Member 4), `group_benchmark.py` (Quan) |
| "The testbed for the final benchmarking is a Google Colab instance running on CPU-only mode." | The notebook `src/notebooks/m1_crawl_group_colab.ipynb` |

---

## 3. Each part in detail

### 3.1. Shared foundation (`common.py`)

- **`DataLayout`**: the single place that defines the paths (`work/`, `corpus/<yymm-id>/`, `logs/`).
  Every module uses it; nobody builds paths on their own.
- **`Manifest`** (`work/paper_manifest.jsonl`): each line is the status of one paper, appended
  rather than overwritten. A later line of the same ID wins over an earlier one, so re-runs and
  interrupted runs never corrupt the file (a half-written line is skipped when reading).
- **`atomic_write_json`**: writes to a temporary file and then renames it, so a sudden shutdown never
  leaves a half-written JSON file.
- **`CsvLog`**: every log line is written to disk immediately, for the report and the benchmark.
- **`RunLock`** (`work/crawler.lock`): two crawlers can never run on the same data folder.

*Why:* safe (idempotent) re-runs are what make it possible to split the benchmark into several
Colab sessions. The lecturer allows this but asks for the logs to be kept: "you may split it
into several sessions, keeping the time logs of each session for checking".

### 3.2. Finding papers and versions (`discovery.py`)

- From the 4 columns of the sheet (`--range 2026-05 12338 2026-05 17337`), it generates the IDs, including
  ranges that span several months.
- It queries the arXiv API with **100 IDs per request** (`id_list`) instead of one ID at a time. 5,000 IDs
  need only about 50 requests, about 2.5 minutes at the 3 s pace.
- The number of versions comes from the ID of the latest version (`...v3` → `v1, v2, v3`).
- **IDs missing from the answer are asked again** (`discovery_recheck`) before concluding
  `not_found`. The arXiv API has returned empty feeds with status 200 (reported in the
  `arxiv.py` library, issue #43, for paginated queries); without asking again, a paper that
  exists but happens to be missing from an answer would be recorded as non-existent.
- `not_found` (arXiv does not have it) is kept apart from `api_error` (an API error, redone on the next run).

### 3.3. Metadata (`metadata.py`)

- The `metadata.json` of each paper: title, authors, submission date, revision dates, venue,
  categories, and **the metadata of each version**, since the statement asks
  "for each version, its paper metadata".
- The latest version is taken from the discovery answer (not asked twice); older versions are
  asked by versioned ID, 100 IDs per request. A paper with only `v1` costs no request.

### 3.4. Rate limiter and handling arXiv refusals (`rate_limiter.py`)

Every request to arXiv goes through a limiter **shared by all threads**, so
no module calls `sleep` on its own:

- **`ARXIV`** (API): 3 s between two requests, one connection, exactly the statement's sentence in the table of section 2.
- **`ARXIV_EXPORT`** (source downloads): following the arXiv bulk-data page,
  "bursts at 4 requests per second with a 1 second sleep, per burst"
  (<https://info.arxiv.org/help/bulk_data.html>), i.e. 2 requests per second on average,
  with several files downloading at once. API calls also count towards this pace, so the total never exceeds it.
  See section 4.1 on how this differs from the statement's wording.
- **HTTP 429**: arXiv staff explain that 429 "relates to server capacity and total
  number of users, not to individual usage" and "the only fix is try again later". On a 429,
  **every** arXiv request therefore pauses together, the pause doubling on each further
  refusal, up to a cap. 429s arriving at the same time on several connections count as one
  (they do not add up).
- **HTTP 503 or a lost connection on one file**: only that file is retried; the other files
  keep downloading. Only when two *different* files fail within 2 minutes is it treated as a general error.
- **arXiv files not delivered for hours** (seen in the October run: a few versions
  returned 503/429 for hours on end, each time stopping the whole crawler, about 30 minutes in
  total): after 2 errors the file is **put aside** for a later pass, without stopping the other files.
- **Large files cut off midway, with arXiv refusing to resume**: the real 5,000-ID run log of
  05/11 has versions with `attempts = 6` that received a round number of MiB and then ended
  with `HTTP 500`, 400–711 s per file: the request for the missing part (`Range`) got
  500 five times in a row, and 500 was not treated like 503, so the file was not put aside;
  most of the 1.07 hours of the *sweep* step was spent waiting for these files. Since this fix:
  a resumed download that gets 500/502/504 drops what it has and downloads the whole file again **once**,
  and a further cut puts the file aside for the next pass; when arXiv refuses `Range` 3 times without
  ever accepting it, the run stops asking for `Range` (log: `[download] arXiv refused 3 resumed downloads ...`);
  500/502/504 on a whole-file request is reported to the limiter like 503 (two errors put the
  file aside). `download_log.csv` gains `route` (the route of the last answer) and
  `first_error` (the first error, for example why a file was cut off).
- **Short waits and probing** (`ddeecb9`): the first wait is 5 s, doubled on each further
  refusal, at most 2 minutes; each normal arXiv answer halves the next wait;
  at the end of a wait only **1 probe request** is sent, and the other connections resume only after a
  normal answer. Reason, from the real 5,200-ID run log: 46% of the time was waiting, including 92
  minutes in a row where each 429 cost a 5-minute wait although arXiv still delivered most files.
- **8 download connections** (set with `KET_NOI` in Step 1 of the notebook, at most 10): a file
  takes about 5.5 s to arrive in full, so 6 connections reach only about 55% of the 2 requests/s pace.
  The pace does not change: more connections do not raise the number of requests per second.
- **Bounded giving up**: when arXiv keeps refusing for 30 minutes (`--max-wait-minutes`), the crawler stops
  with code 3, and everything already finished is saved.
- Every pause is written to `logs/arxiv_throttle_log.csv` for the report.

*Why:* the lecturer grades the error handling ("I will grade how your group handles the error
cases while crawling") and the waiting time counts in the runtime ("the retries and delays
also count in the total runtime, so you also need to think about how to reduce this waiting time").

### 3.5. One-command pipeline and retries in passes (`main.py`)

- `python src/main.py --range ... --data-dir ...` runs discovery → metadata →
  (source download **in parallel with** references). The last two call different servers
  (arXiv and Semantic Scholar), each with its own limits, so running them together breaks no limit
  and saves time.
- **Retries in passes**: a temporary error is not retried on the spot for minutes;
  the pass goes on with other work, and only at its end does a retry pass run for the missing part alone.
  `logs/pass_log.csv` records what is still missing after each pass, to report the success rate
  per pass. The lecturer allows it: "groups may crawl several times, with the number of passes and the
  success rate decided by the group".
- Final results (404 for a withdrawn version, PDF-only versions) are never retried.
- `--limit N` for trial runs, `--stages` to run single steps; Stop stops immediately
  and the next run carries on.

### 3.6. Source download (changes to Member 2's `downloader.py`, `checkpoint.py`, `extractor.py`)

- **Several papers download at once**, one thread per paper, all threads sharing the limiter of section 3.4.
  A file usually takes a few seconds to arrive; downloading one file at a time would never reach the
  allowed pace.
- Papers that failed before are **downloaded last**; a file put aside does not count as a "consecutive error".
- A version recorded as finished whose files are missing on disk is **downloaded again**; a full disk
  **stops the crawler** instead of writing broken files in bulk.

### 3.7. References: several papers in one request (changes to Member 3's `references.py`)

- Semantic Scholar's `POST /paper/batch` gets the references of **several papers
  in one request**, instead of one paper and one page at a time.
- The Semantic Scholar documentation says this endpoint "Can only return up to 9999 citations at
  a time", and beyond that it does not report an error but returns fewer. Each request therefore asks
  for at most 8,000 references, and a paper whose answer has fewer than its `referenceCount` is fetched again
  page by page.
- The Semantic Scholar limit: 1 request/second with a key (statement: "The API enforces limits
  of 1 request per second ... Exceeding these triggers 429 errors, so implement delays
  and error handling").

### 3.8. Submission dates of cited papers from Kaggle (`ref_dates_kaggle.py`)

- Each 5,000 papers cite about 50,000 arXiv papers; asking the arXiv API for their submission dates costs
  about 500–700 requests 3 s apart, competing with the source downloads for the pace.
- The statement says the Kaggle dataset "can be used directly without any API calls". The script
  reads the arXiv metadata file on Kaggle line by line (no full decompression, little RAM)
  and takes the date of `v1`. When Kaggle fails, it falls back to asking arXiv.
- Dates already looked up by a previous member are reused (`--cache`); a paper cited by several members
  is looked up once.

### 3.9. The group benchmark on Colab (`chunked_crawl.py`, `snapshot.py`, `group_benchmark.py`)

The statement requires measuring on Colab, and the lecturer requires measuring on **all of the group's data**
("evaluated on all of the group's data"; "the benchmark results must be run on the complete data"),
while Colab allows at most 12 hours per session. The trial run on Drive also showed that each
re-run spent 9–13 minutes just checking which `metadata.json` files existed, and that partial
downloads deleted on Drive went to the Trash and filled up the Drive.

- **The data lives on the Colab machine's disk** (`/content/...`), not on Drive.
- **Steps of 1,000 papers**, each step running under `benchmark.py` so that time and RAM
  are measured correctly. After each step, the new part is packed into **one `.tar.gz` file** saved to
  Drive (only what changed since the previous save); nothing on Drive is ever overwritten or deleted.
- **A dead runtime carries on**: a new runtime restores the saves and continues from the unfinished
  step; at most one step is lost.
- **One range after the other**, one crawler at a time, because of the arXiv API terms:
  the limits "apply to all of the machines under your control as a whole".
- **The lecturer recommends a joint run**: "I recommend that you put the ID lists together and
  run them jointly to save computing resources".
- **`group_benchmark.py`** merges the logs of every range into one table for the group: time,
  peak RAM, size, number of requests, arXiv waiting time, number of refusals.
  This tool sends no request.

### 3.10. Colab notebooks

- `src/notebooks/m1_crawl_group_colab.ipynb`: the official run for the whole group; fill in the ID range and
  run it. The Semantic Scholar key is kept in Colab *Secrets*, never written in the code.
- `src/notebooks/m1_crawl_trial_colab.ipynb`: a trial run on about 100 IDs before the real run.
- Proxies, WARP and AWS are **optional**, see section 3.11.

### 3.11. Changing the route (IP): Webshare proxies, WARP, AWS API Gateway

Purpose: when arXiv returns 429 to **one** IP address, other requests go on through other IPs
instead of the whole crawler waiting. The basis within the course is the lecturer's answer in the
Q&A (Milestone 3 tab): groups may "apply multi-threading optimisation techniques or VPN IP
switching to get past the rate limit and speed up the download".

All routes **share one limiter** (section 3.4): the total number of requests over all IPs
still follows that pace. The code says it plainly: "more IPs add routes, not speed", that is, more
IPs add backup routes, they do not send faster. All of it is in `rate_limiter.py`.

| Route | Added by | Used for | Turned on with |
| :-- | :-- | :-- | :-- |
| Colab's network | (default) | Every request | Always there |
| **Webshare proxies** (`ProxyPool`) | Quan, `a1582b5` | Rotating the arXiv **API** requests (and large files, last row of the table) | `DUNG_PROXY` in Step 1 + each member's own secret `ARXIV_PROXIES` (never in the code; `proxies.txt` is not committed) |
| **Cloudflare WARP** | Quan, notebook `ca0fe44` | Downloading **source files**: a second IP next to Colab's | `DUNG_WARP` in Step 1; the notebook installs `wgcf` + `wireproxy` (SOCKS5 port `127.0.0.1:40000`), and the crawler picks it up when the port is open |
| **AWS API Gateway** (`requests-ip-rotator`) | Quan, `ddeecb9` | API requests, and by default source files too, go through the gateway of an AWS account | `DUNG_AWS` (and `AWS_TAI_FILE`) in Step 1 + 2 secrets `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`; without the keys, the library or a gateway, it turns itself off |
| **Several Webshare accounts for large files** | Quan, `ddeecb9` | Files AWS cannot carry (> 10 MB) | `WEBSHARE_FILE_LON` in Step 1 (sets `ARXIV_PROXY_LARGE_FILES=1`); needs both AWS and `DUNG_PROXY` |

Accompanying techniques:

- **Pausing each route separately** (Thu, `6ef1619`): a 429 through one route pauses only that route
  (15 s, doubled, at most 5 minutes) while the other routes go on. Only when no route is
  free does every request pause as in section 3.4. Logged as `route_pause` in
  `arxiv_throttle_log.csv`.
- **Quarantining broken proxies** (`_quarantine`): a proxy with a connection error is skipped for 5 minutes,
  and the request is retried through the next route; a proxy error does not count as an arXiv refusal.
- **Switching route at once when AWS cannot carry a file**: API Gateway refuses answers over 10 MB
  and cuts requests after 29 s. The file is then sent again through Colab/WARP after waiting for a new turn,
  and that URL no longer goes through AWS (log `aws_fallback`). Files over 10 MB get
  `HTTP 500 InternalServerErrorException Internal server error` from API Gateway, not 413: the
  5,000-ID run of 06/11 had 610 such errors, all 610 on files of 10–90 MB. The 161 504 errors
  (`Network error communicating with endpoint`, `Endpoint request timed out`) were almost all on
  smaller files. That 500 error on a source file is therefore treated as "over 10 MB": the file takes the
  large-file rotation (with Webshare when `WEBSHARE_FILE_LON` is on), it does not count as an AWS failure (AWS is not
  set aside for 5 minutes), and the other versions of the same paper no longer try AWS either. The gateway is declared
  with the binary type `*/*` so that `.tar.gz` files are not turned into text. When arXiv returns 403 through AWS 3 times
  while Colab/WARP still download, AWS is turned off for the rest of the run.
- **Webshare by account**: IPs with the same user/password are grouped into one account,
  each account being one turn of the rotation, so the bandwidth is used evenly (the free plan has 1 GB/month).
  Webshare returning 402 (out of bandwidth) drops the whole account; 407 drops that IP; 3 dropped IPs of
  one account drop the whole account.

**To know before turning them on:**

- **arXiv terms**: "the following rate limits apply to all of the machines under
  your control as a whole" and "You should not attempt to overcome these limits by
  increasing the number of machines used to make requests". The lecturer allows it within the
  course, but the arXiv terms still say this; the report should state both.
- **Colab**: the Colab FAQ does not allow "connecting to remote proxies". If the runtime is cut
  unexpectedly while proxies/WARP are on, set `DUNG_PROXY` and `DUNG_WARP` to `False`.
- **Cost**: AWS API Gateway charges per request; the free Webshare plan has 1 GB/month.
- **In the repository's notebook**: Step 1 has `DUNG_AWS`, `AWS_TAI_FILE`, `AWS_VUNG`,
  `WEBSHARE_FILE_LON`; Step 4 loads the 2 AWS keys from *Secrets*, installs `requests-ip-rotator` and
  `boto3` (not in `requirements.txt`, installed only when AWS is on) and tests the API Gateway
  permission in each region; a missing gateway is created by `requests-ip-rotator` when the crawler
  starts. Without keys or permissions the crawler still runs normally, just without the AWS route.

#### Turning on the AWS route: what the code needs and what must be respected

In each region of `AWS_VUNG` (the variable `ARXIV_AWS_REGIONS`), `requests-ip-rotator` reuses the
gateway named `https://export.arxiv.org - IP Rotate API`, and creates one when there is none.
An account registered with a university e-mail belongs to the university's AWS Organization: the
Organization's SCP blocks API Gateway in every region except `ap-southeast-2` (measured on 06/11), so
`AWS_VUNG` defaults to `ap-southeast-2`; when it is empty, the library uses its default region list and
stops at the first blocked region. The AWS route is on only when all 3 items below are present; when any of them
is missing, the crawler goes on as if there were no AWS (without keys it prints nothing; with keys but a
failed start-up it prints `[proxy] Lỗi khởi động AWS API Gateway: ...`, "AWS API Gateway start-up error"):

| Needed | Where | Note |
| :-- | :-- | :-- |
| The libraries `requests-ip-rotator` and `boto3` | Installed by the notebook in Step 4 when `DUNG_AWS` is on | Not in `requirements.txt` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Colab *Secrets* (with Notebook access on) | Step 4 passes them to the crawler's environment; *Secrets* do not become environment variables by themselves |
| API Gateway permission in the regions of `AWS_VUNG` | The user's IAM (and the Organization's SCP, if any) | Step 4 tests each region and prints the reason when it is blocked |

The related switches (set in Step 1 of the notebook): `AWS_TAI_FILE = False` (i.e.
`ARXIV_AWS_FILES=0`) sends only API requests through AWS, with source files going through Colab/WARP (default:
source files too); `WEBSHARE_FILE_LON` (i.e. `ARXIV_PROXY_LARGE_FILES=1`) sends files over
10 MB through the Webshare rotation instead of Colab/WARP only.

What must be respected when using AWS:

- **Keys**: only in Colab *Secrets*; never pasted into a code cell, printed or
  committed. Use a separate IAM user with API Gateway permissions only, and delete its keys when
  the group finishes the project. A leaked AWS key lets someone else use the account and charge it.
- **Cost**: API Gateway charges by the number of requests. Watch the AWS Billing page
  during the run; when done, **delete the gateways** created, so that no further charges accrue.
- **Turning it off quickly**: removing the 2 key variables (or not installing the library) stops the crawler
  from using AWS, without any code change.
- **Checking that AWS runs**: with AWS on, start-up prints
  `[proxy] Đã khởi động AWS API Gateway thành công: N vùng ...` ("AWS API Gateway started: N regions"); without this line
  there is no AWS route. During the run, `[AWS] ...: gửi lại qua ...` ("sent again through ...") and `aws_fallback`
  in `arxiv_throttle_log.csv` are files AWS could not carry, downloaded again through Colab/WARP;
  `[AWS] ... tắt AWS cho phần còn lại của bước này` ("AWS turned off for the rest of this step") means arXiv returned 403 through AWS 3 times.

---

## 4. Where the group departs from the statement's wording, and why

### 4.1. Source download pace: the statement says 3 seconds

Statement, source download part: "Point automated downloads at this mirror and never at the
main site arxiv.org ... and use a descriptive User-Agent header with the same
3-second pacing."

The crawler downloads at the pace of the arXiv bulk-data page (2 requests/second on average,
8 files at once), faster than 3 seconds per file. Basis: the lecturer answered in the Q&A that groups may
"apply multi-threading optimisation techniques ... to get past the rate limit and speed up the
download", and arXiv itself suggests the pace "bursts at 4 requests per second" for
`export.arxiv.org`.

For comparison: at exactly 3 seconds per file, each range of 5,000 IDs (about 7,900 versions) takes
about 6.6 hours for the download alone. **The group should state this choice clearly in the report**,
with the two sources above.

### 4.2. The bulk copies on S3 / Google Cloud are not used

The statement mentions `s3://arxiv/src/` (requester-pays) and "A free mirror of the same sources
also exists on Google Cloud Storage at gs://arxiv-dataset".

- **S3**: the downloader pays Amazon for the bandwidth, an AWS account is needed, and the files are grouped
  by month (each package about 500 MB, holding many papers), so single versions cannot be fetched.
  The statement also says "Implementing a full repository-scale harvest is not part of this milestone."
- **Google Cloud `gs://arxiv-dataset`**: checked on 03/11/2026, the folder
  `arxiv/arxiv/` holds only `pdf/`, `ps/`, `html/`, **no TeX sources**, so it cannot be
  used for Milestone 1 (which needs `.tex`).

### 4.3. `.bbl` files

About 9% of the versions (a sample of the first 300 papers of the AQuan range) have their references only
in a `.bbl` file. The statement says "extract only the .tex and .bib files from each source
archive" and "those are the only file types the working corpus keeps", so the first crawl followed the statement
and recorded these versions as a limitation.

*Superseded:* after the lecturer's Q&A answer that the references of such papers must not be lost, `extractor.py`
also keeps `.bbl` files, and `src/fetch_bbl.py` adds them to a corpus crawled earlier (see `README.md` section 8.4).

---

## 5. All switches and parameters

### 5.1. Notebook (Step 1)

| Variable | Default | Meaning |
| :-- | :-- | :-- |
| `CAC_KHOANG` | (must be filled in) | One line per range: its name and the 4 columns of the sheet. The ranges run one after the other |
| `THU_MUC_LUU` | `chinh_thuc` (trial: `chay_thu`) | The Drive folder holding the `.tar.gz` saves and the results |
| `DOAN` | 1000 | Papers per step; a dead runtime redoes only the running step |
| `NGAY_KAGGLE` | `True` | Submission dates of cited papers from Kaggle instead of the arXiv API |
| `CHO_TOI_DA` | 30 | Minutes of continuous arXiv refusals before the step stops (it retries once by itself after 15 minutes) |
| `KET_NOI` | 8 | Files downloading at once, at most 10; the pace stays at 2 requests/second |
| `DUNG_PROXY` | `True` | Rotate Webshare proxies for API requests (needs the secret `ARXIV_PROXIES`) |
| `DUNG_WARP` | `True` | The notebook installs WARP as a second IP for downloading files |
| `DUNG_AWS` | `True` | The AWS API Gateway route (needs the 2 AWS secrets and a usable gateway; skipped otherwise) |
| `AWS_TAI_FILE` | `True` | With AWS: source files < 10 MB go through AWS too (`False`: API requests only) |
| `AWS_VUNG` | `"ap-southeast-2"` | The AWS regions used, separated by commas (a university e-mail account may only use this region) |
| `WEBSHARE_FILE_LON` | `True` | With AWS and proxies: files AWS reports as > 10 MB rotate over Colab + WARP + each Webshare account |
| `CHAP_NHAN_CON_THIEU` | `False` | `True`: when Step 6 returns code 4 (a few files not downloaded yet), carry on to Step 7 anyway |
| `FILE_MOI` | `github` | Take the code from the branch `BRANCH`, or upload new `.py` files not pushed yet |

### 5.2. `main.py` (by hand or inside each step)

| Parameter | Meaning |
| :-- | :-- |
| `--range M1 ID1 M2 ID2` or `--start ID --end ID` | The ID range, written as the 4 columns of the sheet or as two IDs |
| `--data-dir` | The data folder (outside the repository; on Colab use `/content/...`) |
| `--stages` | Run only some steps: `discovery,metadata,download,references` |
| `--limit N` | Trial run on the first N IDs |
| `--batch-size` | IDs per API request (default 100) |
| `--retry-passes` | Number of retry passes for the missing part (default 2) |
| `--max-wait-minutes` | Minutes of continuous arXiv refusals before stopping with code 3 (default 30) |
| `--no-arxiv-dates` | Do not ask arXiv for the submission dates of cited papers (`ref_dates_kaggle.py` fills them in) |
| `--force`, `--user-agent` | Redo finished steps; change the User-Agent sent to arXiv |

### 5.3. Environment variables

| Variable | Meaning |
| :-- | :-- |
| `S2_API_KEY` | Each member's own Semantic Scholar key (1 request/second with a key) |
| `ARXIV_CONNECTIONS` | Files downloading at once (set by the notebook from `KET_NOI`) |
| `ARXIV_PROXIES` | The list of Webshare proxies, separated by commas (read by the notebook from the secret) |
| `WARP_PORT` | The SOCKS5 port of WARP (default 40000) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Turn on the AWS route (section 3.11) |
| `ARXIV_AWS_FILES` | `0`: source files do not go through AWS |
| `ARXIV_PROXY_LARGE_FILES` | `1`: files over 10 MB take the Webshare rotation |

---

## 6. Reading the logs to see which techniques ran

Most files are in `<data-dir>/logs/` (on Drive: inside the save of each range);
`chunk_journal.jsonl` is in `<data-dir>/work/`, `snapshot_log.csv` in the save folder on Drive.

| File | One row per | Used for |
| :-- | :-- | :-- |
| `run_log.csv` | step of a run (start, end, seconds) | The time of each step |
| `arxiv_api_log.csv` | arXiv API request | Number of requests, time, API errors |
| `download_log.csv` | processed version | Status, bytes downloaded and kept, number of attempts |
| `references_log.csv` | paper per references attempt | References status, how they were fetched (`/paper/batch` or page by page) |
| `pass_log.csv` | pass | What is still missing after each pass, for the success rate per pass |
| `arxiv_throttle_log.csv` | arXiv refusal or missing answer | Waiting time, see the table below |
| `work/chunk_journal.jsonl`, `snapshot_log.csv` | step of `chunked_crawl.py`, Drive save/restore | Which steps finished, how long the Drive saves took |

The `event` column of `arxiv_throttle_log.csv`:

| `event` | Meaning |
| :-- | :-- |
| `pause` | Every arXiv request pauses together for a while (column `pause_seconds`) |
| `isolated` | One file failed on its own; the other files keep downloading |
| `defer` | A file failing for the second time is put aside for a later pass |
| `recover` | arXiv answers normally again: the next wait goes back to 5 s |
| `give_up` | Refused continuously for longer than `--max-wait-minutes`: stop with code 3 |
| `route_pause` | Only one route (Colab, WARP, AWS, a proxy) pauses; the other routes go on |
| `aws_fallback` | AWS could not carry a file (over 10 MB, over 29 s...): downloaded again through Colab/WARP |

`group_benchmark.py` merges these files for the whole group (columns `arxiv_pause_seconds`,
`arxiv_pauses`, `arxiv_deferred`, `arxiv_route_pauses`...).

---

## 7. Common issues

| Seen | Meaning | Action |
| :-- | :-- | :-- |
| `Crawler return code: 0` | Every range finished | Go to Step 7 |
| Code **4** | Finished, a few arXiv files not delivered yet | *Run all* again 1–2 hours later: only the missing part is done |
| Code **3** | arXiv kept refusing (it already retried once after 15 minutes) | *Run all* again 1–2 hours later |
| Code **130** | Stop was pressed | *Run all* again when wanted: it carries on from the unfinished step |
| Code **1** | A step failed twice, or a Drive save failed | Read the error line just above; if the Drive is full, free space and empty the Trash |
| `[arXiv] HTTP 429 ... pausing all arXiv requests for N s` | arXiv is overloaded | Normal, let it run |
| `... put aside for a later pass` | An arXiv file not delivered yet was put aside | Normal, the next pass downloads it |
| `[proxy] ... bỏ qua 5 phút` ("skipped for 5 minutes") | That proxy has a connection error | If the same proxy keeps appearing, remove it from the secret |
| The runtime disconnects while proxies/WARP are on | Colab may be blocking proxy connections | Set `DUNG_PROXY`, `DUNG_WARP` to `False` |
| Step 2 prints `0 bản lưu` ("0 saves") although it ran before | Step 1 was changed, or the wrong Drive account is used | Stop at once and fix Step 1, or the crawl starts again from scratch |

---

## 8. Real measurements for the report

Thu's 5,200-ID run (30/10/2026, the old way: data on Drive, 4 connections):

| Measure | Value |
| :-- | :-- |
| Total time | 23,513 s (6.53 hours) |
| Versions | 7,873 (7,307 downloaded, 538 PDF only, 28 not found) |
| Waiting time due to arXiv refusals (429) | 10,815 s (**46%**) |
| Requests to arXiv | 9,696 (8,376 file downloads, 1,320 API) |
| Size downloaded / kept (.tex, .bib) | 34.0 GB / 1.84 GB |
| Peak RAM | 481 MB |

Almost half of the time was spent waiting for arXiv, and the refusals varied with the time of day,
independently of the crawler; this is the main point when explaining the runtime in the report.
arXiv staff also say that 429 comes from the server's overall load.

---

## 9. Quick trial

```bash
pip install -r src/requirements.txt
python src/main.py --range 2026-05 12338 2026-05 17337 --data-dir data --limit 10
python src/validate.py --data-dir data
```

On Colab: open `src/notebooks/m1_crawl_trial_colab.ipynb`, fill in the name, the ID range and `TU`, `DEN`
(about 100 IDs), then run it from top to bottom.
