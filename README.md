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

  
## 7. Data Validation and Performance Analysis (Role 4)

Role 4 provides three modules for validating the collected
corpus, measuring crawler performance, and computing statistics
for the Milestone 1 report.

### 7.1. Data Validation

Run the validation tool after collecting data:

```bash
python src/validate.py --data-dir data
```

Output:

- `data/logs/validation_report.csv`

Validation results include:

- PASS: the check passed.
- WARN: the result requires further inspection.
- PENDING: data is missing or processing is incomplete.
- FAIL: the data failed a validation check.

Validation covers paper metadata, version coverage,
source files, and reference data.

### 7.2. Corpus Statistics

Generate statistics from the current corpus:

```bash
python src/corpus_statistics.py --data-dir data
```

Output:

- `data/logs/summary_statistics.csv`

The statistics include paper coverage, version coverage,
source statuses, reference completeness, and data sizes.

The module reads the stored corpus state and avoids
double-counting historical retry records.

### 7.3. Performance Benchmark

Run a benchmark on Google Colab with CPU-only runtime:

```bash
python src/benchmark.py \
  --data-dir data \
  --interval 2 \
  --disk-interval 30 \
  -- python src/main.py \
  --start START_ID \
  --end END_ID \
  --data-dir data
```

Replace START_ID and END_ID with the assigned arXiv IDs.

Benchmark outputs:

- `performance_samples_<run_id>.csv`
- `performance_summary_<run_id>.json`

These files are stored under `data/logs/`.

The benchmark records end-to-end wall time, sampled RAM
usage, and sampled data-directory size.

Sampled peaks may not capture short-lived resource peaks.

For interrupted runs, each execution must be reported
separately. A resumed run is not equivalent to a fresh
full-pipeline benchmark.

### 7.4. Data and API Limitations

The collected corpus and logs are stored outside the
source-code repository.

Semantic Scholar API errors, including HTTP 429, are
recorded as incomplete reference collection rather than
successful results.

Source HTTP 404 responses are recorded separately and
should not automatically be classified as PDF-only.

The validation and statistics outputs describe the
current dataset state; they do not guarantee that all
papers and references were successfully collected.

