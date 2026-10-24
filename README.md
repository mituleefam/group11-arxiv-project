# arXiv Crawler — Milestone 1 (Group 11)

Crawls the arXiv papers of an assigned ID range: every version's LaTeX source
(`.tex` / `.bib` only, figures removed), the paper metadata, and the reference
metadata from Semantic Scholar.

```
src/
├── main.py          pipeline entry point: discovery -> metadata -> (download || references)
├── discovery.py     resolve the assigned IDs and their versions with the arXiv API
├── metadata.py      metadata of every version
├── downloader.py    download the source of every version
├── extractor.py     detect the source format
├── checkpoint.py    per-version state for resuming
├── references.py    references from the Semantic Scholar Graph API 
├── rate_limiter.py  shared arXiv limiter: 3s between requests, one connection at a time
├── common.py        data layout, status codes, manifest, CSV logs
└── requirements.txt
environment.yml      conda environment (installs src/requirements.txt)
```

## 1. Setting up the environment

Python 3.11 is used (3.10+ works). Choose **one** of the two ways.

### Option A — conda

```bash
conda env create -f environment.yml
conda activate arxiv
```

### Option B — pip (`requirements.txt`)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r src/requirements.txt
```

On Google Colab only the install line is needed:
```python
!git clone https://github.com/mituleefam/group11-arxiv-project.git
%cd group11-arxiv-project
!pip install -r src/requirements.txt
```

### Semantic Scholar API key (optional)

Without a key the references stage spaces its requests 3 s apart and often
receives HTTP 429; with a key it uses 1 request per second. **Never commit the
key.** Put it in an environment variable named `S2_API_KEY`:

```bash
export S2_API_KEY=...                         # Linux / macOS
set S2_API_KEY=...                            # Windows cmd
```

```python
# Colab: store it as a secret named S2_API_KEY, then
import os
from google.colab import userdata
os.environ["S2_API_KEY"] = userdata.get("S2_API_KEY")
```

## 2. Running the whole pipeline (`main.py`)

Run from the repository root. The stages run in this order:

1. **discovery**: which IDs of the range exist and how many versions each has;
2. **metadata**: `metadata.json` for every paper;
3. **download** and **references**, at the same time (they call different
   servers, each with its own rate limit).

```bash
# the four columns of the registration sheet: start_month start_id end_month end_id
python src/main.py --range <start_month> <start_id> <end_month> <end_id> --data-dir data

# For example:
python src/main.py --range 2026-04 05535 2026-04 10534 --data-dir data

# the same range written as two IDs
python src/main.py --start 2604.05535 --end 2604.10534 --data-dir data

# trial run on the first 10 IDs
python src/main.py --start 2604.05535 --end 2604.10534 --data-dir data --limit 10

# only some stages
python src/main.py --start 2604.05535 --end 2604.10534 --data-dir data --stages discovery,metadata
```

| Argument | Default | Meaning |
|---|---|---|
| `--range START_MONTH START_ID END_MONTH END_ID` | — | The range as in the registration sheet, e.g. `2026-04 05535 2026-04 10534`. May span several months. |
| `--start ID` / `--end ID` | — | The same range as two IDs (`2604.05535` or `2026-04:05535`). Use either `--range` or `--start`/`--end`. |
| `--data-dir DIR` | `data` | Root folder of the corpus, working files and logs. Keep it outside the repository (e.g. on Google Drive); `data/` is git-ignored. |
| `--stages LIST` | all | Comma-separated subset of `discovery,metadata,download,references`. |
| `--limit N` | none | Trial run: only the first N IDs of the range. |
| `--batch-size N` | `100` | arXiv IDs per API call in discovery and metadata. |
| `--user-agent TEXT` | `NMKHDL-arxiv-crawler/1.0 (university course project)` | User-Agent header sent to arXiv. |

**Interruptions.** Re-running the same command is safe and continues where it
stopped: finished IDs, versions and papers are skipped and never overwritten;
failed ones are tried again. `main.py` exits only when every selected stage has
finished, so after `[download] finished` it may still be waiting for the
references stage.

## 3. Running a single stage

The discovery stage must have run once (it writes `work/paper_manifest.jsonl`,
which the other stages read).

| Stage | Command |
|---|---|
| Discovery | `python src/main.py --start 2604.05535 --end 2604.10534 --data-dir data --stages discovery` |
| Metadata | `python src/main.py --start 2604.05535 --end 2604.10534 --data-dir data --stages metadata` |
| Source download | `python src/downloader.py --data-dir data` |
| References | `python src/references.py --data-dir data` |

`downloader.py` options:

| Argument | Meaning |
|---|---|
| `--data-dir DIR` | Same folder as `main.py --data-dir`. |
| `--ids ID [ID ...]` | Only these papers (they must be in the manifest), e.g. `--ids 2604.05700`. |
| `--limit N` | Only the first N papers. |

`references.py` options:

| Argument | Meaning |
|---|---|
| `--data-dir DIR` | Same folder as `main.py --data-dir`. |
| `--ids ID [ID ...]` | Only these papers (no manifest needed). |
| `--limit N` | Only the first N papers. |
| `--retry STATUSES` | Crawl again papers that ended with these final statuses, e.g. `not_found,unavailable`. |
| `--no-arxiv-dates` | Skip the arXiv calls that fill `ref_submission_date` (filled on a later run). |

## 4. Output

```
<data-dir>/
├── work/
│   ├── paper_manifest.jsonl     one record per ID: status and version list
│   ├── ref_arxiv_dates.json     cache of arXiv submission dates of cited papers
│   └── downloads/               temporary .part files while downloading (normally empty)
├── corpus/<yymm-id>/            e.g. corpus/2604-05535/
│   ├── metadata.json            title, authors, submission/revised dates, venue, per-version metadata
│   ├── versions.json            download state of every version
│   ├── v1/, v2/, ...            .tex/.bib files with the archive's own sub-folders
│   ├── references_all.json      every reference returned by Semantic Scholar
│   └── references.json          references with an arXiv ID
└── logs/                        CSV logs, see below
```

## 5. Log tables

All logs are CSV files in `<data-dir>/logs/`, appended row by row (nothing is
lost if a run stops). Times are UTC ISO 8601, durations in seconds, sizes in bytes.

### `run_log.csv` — one row per stage and run

| Column | Meaning |
|---|---|
| `run_id` | Start time of the run (groups the stages of one command). |
| `stage` | `discovery`, `metadata`, `download` or `references`. |
| `started_at`, `finished_at` | Wall-clock start and end of the stage. |
| `seconds` | Duration of the stage. |
| `items` | IDs resolved (discovery), metadata files written (metadata) or papers given to the stage. |
| `note` | Warning or error, e.g. papers left incomplete. |

### `arxiv_api_log.csv` — one row per arXiv API call

| Column | Meaning |
|---|---|
| `time` | When the call finished. |
| `stage` | `discovery`, `metadata` or `ref_dates` (submission dates of cited papers). |
| `first_id`, `last_id` | First and last ID of the batch. |
| `requested`, `returned` | IDs asked and entries received. |
| `attempts` | HTTP attempts (retries included). |
| `seconds` | Duration of the call, waits included. |
| `error` | Empty on success. |

### `download_log.csv` — one row per processed version

| Column | Meaning |
|---|---|
| `time` | When the version was finished. |
| `arxiv_id`, `version` | e.g. `2604.05535`, `v1`. |
| `status` | `success`, `pdf_only`, `no_tex`, `not_found` (HTTP 404) or `failed` (retried on the next run). |
| `source_format` | Detected format: `tar.gz`, `tar`, `gzip-tex`, `plain-tex`, `pdf`, `docx`, `postscript`, `zip`... |
| `http_status` | Last HTTP status received. |
| `attempts` | HTTP requests made (retries and resumed downloads included). |
| `download_seconds`, `extract_seconds`, `total_seconds` | Time spent downloading, extracting, and in total. |
| `downloaded_bytes` | Size of the file received from arXiv (compressed). |
| `original_bytes` | Total **uncompressed** size of every file in the source. |
| `image_bytes` | Part of `original_bytes` that are figure files. |
| `kept_bytes` | Total size of the kept `.tex`/`.bib` files (uncompressed, same basis as `original_bytes`). |
| `kept_files`, `tex_files`, `bib_files` | Number of kept files, of which `.tex` and `.bib`. |
| `skipped_files` | `.tex`/`.bib` members not written (unsafe path, link, name refused by the file system); details in `versions.json`. |
| `error` | Empty on success. |

Size reduction from removing figures: `1 - kept_bytes / original_bytes`.

### `references_log.csv` — one row per paper and attempt

| Column | Meaning |
|---|---|
| `time` | When the paper was finished. |
| `arxiv_id` | Citing paper. |
| `status` | `success`, `empty` (no reference listed), `unavailable` (references exist but are not shared), `not_found` (not indexed) or `api_error` (retried on the next run). |
| `s2_paper_id` | Semantic Scholar ID of the paper. |
| `s2_reference_count` | Number of references Semantic Scholar reports. |
| `n_references` | References actually received. |
| `n_with_arxiv_id`, `n_unique_arxiv_id` | References with an arXiv ID, and distinct arXiv IDs. |
| `n_complete`, `n_incomplete`, `n_unresolved` | References by metadata quality (see `references.py`). |
| `pages` | Pages of the `/references` endpoint read. |
| `attempts` | HTTP attempts. |
| `seconds` | Time spent on the paper. |
| `error` | Empty on success. |

## 6. Rate limits

- **arXiv:** at most one request every 3 seconds and one connection at a time,
  enforced by the shared `rate_limiter.ARXIV` for every module (discovery,
  metadata, source download, reference dates). Sources are fetched from
  `export.arxiv.org`, never from `arxiv.org`.
- **Semantic Scholar:** 1 request per second with an API key, one every 3.1 s
  without (100 requests per 5 minutes); HTTP 429 and 5xx are retried with
  exponential backoff.

  
## 7. Data Validation, Statistics, and Benchmarking (Role 4)

Role 4 **audits and measures** the Milestone 1 crawler. These scripts do not modify or repair paper source files or silently change the outcomes of arXiv/Semantic Scholar requests. Run them from the repository root. **Keep raw data, logs, API keys, Google Drive credentials, and `.env` files out of GitHub.**

### 7.1. Role 4 source files and responsibilities

| File | Function | Writes to the data directory? | Requires network/API key? |
| --- | --- | --- | --- |
| `src/validate.py` | Audit manifest, official IDs, metadata, every version's source files, `kept_files`, reference documents, incomplete arXiv dates, and leftover temporary files. | Yes: `logs/validation_report.csv` (unless `--output` overrides). Does not repair corpus. | No |
| `src/corpus_statistics.py` | Compute paper/version coverage, source sizes, collection and metadata success rates, reference counts with explicit denominators, Discovery time logged, and optional timing metrics. | Yes: `logs/summary_statistics.csv`. | No |
| `src/benchmark.py` | Launch and monitor a command; sample process-tree RAM, remote data-directory size, local disk use, end-to-end invocation wall time and progress/heartbeat. | Yes: `performance_*` logs. | Only if the child command uses an API |
| `src/benchmark_runs.py` | Summarize multiple benchmark invocations and detect heartbeat files with no finished summary. Distinguish new runs from resumed/incomplete runs. | Yes: `benchmark_runs_overview.csv` and `.json`. | No |
| `src/per_paper_benchmark.py` | Run an independent **fresh full-pipeline** experiment for each selected paper and report its observed elapsed wall time. | Yes: dedicated corpus folders, `paper_timings.csv` and `per_paper_summary.json`. | **Yes**, when actually running arXiv/Semantic Scholar |
| `src/test_role4.py` | Offline regression tests for the tools and input/output validation contracts. | Temporary test directories only. | No |

**Other owners:** `main.py`, Discovery, Metadata, Download/Extraction/Checkpoint and Semantic Scholar crawling remain owned by their respective members. A Validation warning does not automatically mean Role 4 caused the error.

### 7.2. Clone and install

Requirements: Python 3.11 or a Python version supported by the current group's dependencies, Git, and access to the shared repository. Do not assume an old Colab runtime will have the same versions as your laptop.

```bash
# Clone once. Replace the URL if the team renames its repository.
git clone https://github.com/mituleefam/group11-arxiv-project.git
cd group11-arxiv-project

# Create an isolated environment (Windows PowerShell: .venv\\Scripts\\Activate.ps1)
python -m venv .venv
# macOS/Linux only:
source .venv/bin/activate

python -m pip install -r src/requirements.txt
python -m pip check
python -m unittest discover -s src -p 'test_role4.py' -v
```

On Windows PowerShell, replace the `source ...` line with `.\.venv\Scripts\Activate.ps1`. `psutil` is required by the benchmark tools; keep it in `src/requirements.txt`. If `pip check` reports a Colab dependency conflict, resolve the pinned versions with the group and rerun tests. Do not ignore dependency warnings during the official performance measurement.

**Existing clone:** Before pulling, save/commit your uncommitted work on your own branch. Then run `git fetch origin`, update your local `main` with `git switch main` and `git pull --ff-only origin main`, and return to your feature branch as needed. Do not overwrite other members' uncommitted files.

### 7.3. Data layout and Semantic Scholar compatibility

Point all Role 4 tools to the exact same **DATA_DIR** used by the crawler:

```text
DATA_DIR/
├── work/
│   ├── paper_manifest.jsonl
│   ├── ref_arxiv_dates.jsonl       # new Member 3 append-only cache
│   └── ref_arxiv_dates.json        # optional legacy cache; still readable by Member 3
├── corpus/
│   └── <yymm-id>/
│       ├── metadata.json
│       ├── versions.json
│       ├── v1/, v2/, ...           # retained .tex/.bib trees
│       ├── references_all.json    # complete S2 reference list and arxiv_dates_complete flag
│       └── references.json        # only cited papers carrying an arXiv ID
└── logs/
    ├── run_log.csv
    ├── download_log.csv
    ├── references_log.csv
    └── ...
```

Member 3's revised `references.py` saves the same two reference JSON structures as before. Reference metadata uses `ref_arxiv_id`, `ref_title`, `ref_authors`, `ref_submission_date`, `ref_s2_id`, and `metadata_status` (`complete` / `incomplete` / `unresolved`), plus supplementary fields. `arxiv_dates_complete` is a **separate boolean**: `success` for S2 references does **not** imply the cited arXiv submission dates have been filled. Role 4 compares both the keys **and the values** of `references.json` against `references_all.json`, and its strict full-pipeline completion metric requires `arxiv_dates_complete=true`.

Reference statuses must be distinguished: `success` (nonempty references), `empty` (genuine zero), `unavailable`, `not_found`, and `api_error` (API failure; Member 3 deliberately does not write a finished JSON). A missing reference JSON after HTTP 429 is **unknown**, not zero. When using `--no-arxiv-dates` in Member 3, the arXiv date requirement remains outstanding; do **not** treat such an experiment as a completed full-pipeline per-paper benchmark.

An API key is not needed for Role 4's **offline** commands. When a real pipeline needs Semantic Scholar, ask the group to supply `S2_API_KEY` via environment variables or Colab Secrets, **never in code/README/commit**. Member 3 manages rate limiting, retries and the pending-date worker.

### 7.4. Validate the existing corpus (no HTTP calls)

First inspect a small sample without overwriting the full validation report:

```bash
python src/validate.py --data-dir DATA_DIR \
  --max-papers 20 --progress-every 5 \
  --output validation_smoke_20.csv
```

Then, **after all crawler processes stop**, validate the full dataset:

```bash
python src/validate.py --data-dir DATA_DIR \
  --expected-count 5000 --finished --progress-every 100
```

`5000` is an illustrative **per-member range size**, not a hard-coded requirement. Checking a count cannot establish that the IDs match the official assignment. For that, have the group supply `official_ids.txt` with one valid arXiv ID per line, and instead run:

```bash
python src/validate.py --data-dir DATA_DIR \
  --assigned-ids official_ids.txt --finished --progress-every 100
```

Report: `DATA_DIR/logs/validation_report.csv`, columns `arxiv_id,version,check_name,status,error_message`.

| Check status | Interpretation |
| --- | --- |
| `PASS` | The particular check passed. **Not** a guarantee every requirement is met. |
| `WARN` | Requires an explanation or manual review, e.g. pending cited arXiv dates or a verified 404 exception. |
| `PENDING` | Missing or incomplete work, e.g. S2 HTTP 429 caused a missing references document. |
| `FAIL` | Structural/content validation failure (missing files, zero-byte source, inconsistent reference values). |

Use `--strict` only for an audit gate: it returns a nonzero exit code if any `PENDING` or `FAIL` exists. Do **not** relabel 404 or missing references just to force a green result. `--verified-404 reviewed.csv` requires genuine prior manual verification and does not recover the source. `--verify-hashes` is meaningful only when Member 2 has recorded archive-time SHA-256 hashes; hashing an already-written file alone is not provenance verification.

### 7.5. Corpus statistics (no HTTP calls)

```bash
python src/corpus_statistics.py --data-dir DATA_DIR
# Stronger when an independently verified ID file is available:
python src/corpus_statistics.py --data-dir DATA_DIR --assigned-ids official_ids.txt
```

Output: `DATA_DIR/logs/summary_statistics.csv` (`metric,value,unit,description`). Important distinctions:

- `papers_pipeline_core_complete`: completed source/metadata/S2 reference collection; cited arXiv dates may still be pending.
- `papers_pipeline_complete_strict`: also requires `arxiv_dates_complete=true` (Member 3's new schema).
- `reference_collection_success_rate_found`: papers with `success` or true `empty` references / discovered papers.
- `reference_metadata_complete_rate`: complete **S2 metadata per reference**, not the proportion of papers with citation dates.
- `mean_references_per_references_document`: conditional average over valid `success` or `empty` documents, **including genuine empty documents as zero**. Papers with HTTP 429 are not silently counted as zero.
- `discovery_total_logged_stage_seconds`: time reported in completed stage logs; interruptions may leave missing measurements.

If Member 3 changes source/reference fields, re-run validation and examine the output schema before reporting final statistics. Do not sum CSV retry rows as if each row were a different paper.

### 7.6. Benchmark and how to interpret it

**Test the instrumentation without using the network:**

```bash
python src/benchmark.py --data-dir /tmp/role4_smoke \
  --interval 1 --disk-interval 5 --heartbeat-seconds 3 \
  -- python -c "import time; time.sleep(4)"
python src/benchmark_runs.py --logs-dir /tmp/role4_smoke/logs
```

The four-second sleep proves that the wrapper can record observations; it **is not a crawler benchmark**. On Windows, replace `/tmp/role4_smoke` with a writable Windows path.

**Final experiment: run on Google Colab, CPU-only** (Runtime → Change runtime type → Hardware accelerator: None). Mount the data location. The crawler must execute within an agreed arXiv/S2 request schedule:

```bash
python src/benchmark.py --data-dir DATA_DIR \
  --interval 2 --disk-interval 300 --heartbeat-seconds 60 \
  -- python src/main.py \
  --start START_ID --end END_ID --data-dir DATA_DIR
```

Keep `START_ID`, `END_ID` and `DATA_DIR` consistent, and use a **fresh directory** to report a fresh end-to-end crawl. A run with `--limit` or without the references stage is only a partial test. If the Colab runtime stops, restart the crawler with the **same** data directory for checkpoint/resume, but record it as a **separate benchmark invocation**. Keep all invocation summaries and their run IDs. Then:

```bash
python src/benchmark_runs.py --logs-dir DATA_DIR/logs
```

Benchmark outputs (under `DATA_DIR/logs`): `performance_samples_<run_id>.csv`, `performance_progress_<run_id>.json`, `performance_summary_<run_id>.json`, and `benchmark_runs_overview.csv/.json`. Progress files from crashed invocations are **partial evidence**, not finished benchmarks.

Report **invocation** wall time separately from the total project duration. Sampled RAM mean/peak and disk measurements have sampling and remote-Google-Drive I/O limitations; the local Colab disk-used metric is not necessarily the bytes created by the crawler. Do not call the sum of overlapping runs or resumed runs a single fresh full-pipeline wall time.

### 7.7. Fresh end-to-end timing of individual sample papers

When APIs are operational and the team has approved the requests:

```bash
python src/per_paper_benchmark.py \
  --output-root FRESH_SAMPLE_DIR \
  --ids PAPER_ID_1 PAPER_ID_2 \
  --interval 5 --disk-interval 300
```

The command runs an independent, fresh *entire pipeline* per paper. It writes `paper_timings.csv` and `per_paper_summary.json`. `complete` requires valid source, Semantic Scholar data **and** completed cited arXiv dates. An S2 result without completed arXiv dates is marked incomplete, not successful.

This is **not** the observed per-paper latency inside a concurrent 25,000-paper crawl. With a measured sample CSV, optionally add its distribution to the statistics:

```bash
python src/corpus_statistics.py --data-dir DATA_DIR \
  --paper-timings FRESH_SAMPLE_DIR/paper_timings.csv \
  --benchmark-summary DATA_DIR/logs/performance_summary_SELECTED_RUN_ID.json
```

Choose only a benchmark summary matching the experiment being described; do not divide a resumed wall time by all assigned IDs. If the report needs true per-paper latency during bulk parallel crawling, Member 1–3 must coordinate event/timestamp instrumentation within `main.py` and the crawling stages; Role 4 can define the measurement and check its logs.

### 7.8. Team workflow, hand-off and Milestone 1 report

1. **Coder:** fetch latest approved code, run offline unit tests and a small end-to-end smoke test, and preserve the Git commit SHA.
2. **Crawler operator:** run on one authorized Colab CPU instance using the official assigned ID ranges; keep source checkpoints and log each interrupted/resumed invocation.
3. **Role 4:** validate the stored corpus only after it is quiescent, compute statistics, and consolidate benchmark summaries. Never invent missing reference data, change `not_found` to `pdf_only`, or change file content to silence Validation.
4. **Other owners:** Member 2 resolves source integrity/missing-file problems; Member 3 resolves S2 HTTP 429 and incomplete `ref_submission_date` acquisition; Member 1 supplies the official ID mapping and coordinates timing if required.
5. **Report owner:** generate tables/figures **during Jupyter Book build** in the separate report repository, showing denominators, sample sizes, environment, Git commit, run IDs, interruptions, unresolved exceptions and distinct wall/RAM/disk measures.

**Delivery:** commit source code and documented commands to this repository, and publish the separate Jupyter Book report. Keep the crawled dataset and large logs off the source GitHub repository. The README explains how to reproduce the code; it is not a claim that all source 404 / S2 429 cases were fixed.
