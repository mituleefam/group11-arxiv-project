# M3 – Member 1: loading the data, cleaning, candidate pairs, split

This part turns two raw tables into the shared input of the group:

- the `bibtex` table of M2 (the entries of each publication, typed by the authors and therefore messy),
- the references of M1 (Semantic Scholar, keeping only the references with an arXiv ID).

It produces two cleaned tables, in the same form on both sides, and the table of (entry, reference) pairs inside each publication. Member 2 (labelling), Member 3 (features) and Member 4 (training) all read these files.

The code is in `src/m3/` and only needs `pyarrow` (already in `requirements.txt`).

## Running

```bash
python src/m3/pipeline_m3.py --tables M2_TABLES --data-dir M1_DATA_DIR [M1_DATA_DIR ...] --work WORK
```

- `M2_TABLES`: the `tables/` folder written by `pipeline_m2.py` (with `bibtex/*.parquet`), for example `/content/m2_work/_merged/tables`.
- `M1_DATA_DIR`: a restored M1 data folder (with `corpus/<yymm-id>/references_all.json`), for example `/content/g11/GBao_2604-05535_2604-10534`. List every range when there are several.
- `WORK`: the M3 working folder; every file below is written there.

The command runs `m3_data.py` → `candidates.py` → `split.py` (once a label file exists) → the later steps of Members 3 and 4, in order. A step whose script is missing prints "skipped" and the pipeline goes on. Each step can also run alone:

```bash
python src/m3/m3_data.py --tables M2_TABLES --data-dir M1_DATA_DIR --out WORK
python src/m3/candidates.py --work WORK
python src/m3/split.py --labels WORK/labels.csv --members 5 --seed 11 --stats WORK/paper_stats.csv --out WORK/splits.json
python src/m3/snapshot_diff.py --old WORK_OLD --new WORK_NEW --out diff.csv --labels WORK/labels.csv
python src/tests/m3/test_data_candidates_split.py
```

Trial on Bao's range (5,000 publications) on an ordinary computer: loading and cleaning take about 45 s, building the pairs 6 s.

## Output files (in `WORK`)

| File | One row per | Key |
|---|---|---|
| `entries_clean.parquet` | BibTeX entry | `(arxiv_id, citation_key)` |
| `references_clean.parquet` | reference with an arXiv ID | `(arxiv_id, ref_arxiv_id)` |
| `candidates.parquet` | (entry, reference) pair of the same publication | `(arxiv_id, citation_key, ref_arxiv_id)` |
| `paper_stats.csv` | publication: m, n, m×n and a few numbers that help choose the publications to label | `arxiv_id` |
| `papers_refs.csv` | publication: Semantic Scholar status, number of references, number of references with an arXiv ID | `arxiv_id` |
| `splits.json` | `{"train": [...], "valid": [...], "test": [...]}` | |

Every publication ID and reference ID uses the dotted form (`2604.05536`, `1706.03762`; old-style IDs such as `hep-th/9901001`), as in the `bibtex` table.

### Columns of `entries_clean.parquet`

| Column | Meaning |
|---|---|
| `entry_type`, `title`, `authors`, `journal`, `url` | kept as in the `bibtex` table, so that the labelling tool shows them to the labeller |
| `title_clean` | the cleaned title (see below); `None` when there is no title |
| `title_tokens` | the words of the title, stop-words removed |
| `authors_last` | the surname of each author, in order |
| `first_author_last` | the surname of the first author |
| `year` | the year (integer) |
| `doi` | the normalised DOI; from the `doi` column, otherwise from a `doi.org` link in `url` |
| `arxiv_id_in_entry` | the arXiv ID written in the entry itself, normalised |
| `arxiv_id_from` | the column where that ID was found (`url`, `journal`, `doi`, `title`) |

### Columns of `references_clean.parquet`

| Column | Meaning |
|---|---|
| `ref_title`, `ref_authors`, `ref_submission_date`, `ref_s2_id` | kept as they are: exactly the columns of the `references` table of the M1 statement |
| `title_clean`, `title_tokens`, `authors_last`, `first_author_last` | cleaned exactly as on the entry side |
| `year` | the year of the arXiv v1 submission (from `ref_submission_date`) |
| `year_s2` | the year recorded by Semantic Scholar (`ref_year`, usually the year of the journal publication) |
| `doi` | the DOI known to Semantic Scholar (86% of the references have one); the form `10.48550/arxiv.<id>` is arXiv's own DOI |
| `venue` | the venue recorded by Semantic Scholar |

## Ideas and method

### 1. Loading the data (`m3_data.py`)

- Entries: the Parquet files of the `bibtex` table are read directly.
- References: the `references_all.json` of each publication is read, **keeping only the rows with a `ref_arxiv_id`**. The statement describes the `references` table as "keeping the arXiv-keyed references only"; a reference without an arXiv ID can never be the answer.
- Why `references_all.json` and not `references.json` as in the task assignment: both files hold the same arXiv references (compared over all 5,000 publications: a 100% match), but `references.json` has no year, DOI or venue, and its keys use the form `2511-23122`. Member 2's DOI rule and Member 3's year feature need those columns. The other file can still be chosen with `--source subset`.
- **The data is not edited**: the original columns stay as they are and the cleaned columns are added next to them. Anything unusual is printed as a report, not patched (see "Data errors found").

### 2. Cleaning (`text_clean.py`)

Principle: **both sides go through exactly the same function**, because the slightest difference in normalisation makes "Attention is all you need" and "Attention is All you Need" different.

**Title** (`title_clean`):

1. LaTeX removed:
   - accents `\"a` → `a`, `\c{c}` → `c`;
   - Greek letters `\alpha` → `alpha` (and `α` → `alpha`, so that `$\alpha$-synuclein` matches `α-synuclein`);
   - other commands lose their name and keep their content: `\emph{Deep}` → `Deep`;
   - `$`, `{}`, `~` removed.
2. Accents of foreign letters removed: `Kähler` → `Kahler`, `Łukasz` → `Lukasz`.
3. Lower case; apostrophes removed altogether (`O'Brien` → `obrien`); every other punctuation mark (hyphen, colon, brackets...) becomes a space, and spaces are collapsed.

Example: `K\"ahler-Einstein metrics: $U(1)$` → `kahler einstein metrics u 1`.

Semantic Scholar titles also still contain LaTeX (`$` in 525 titles, `\` in 282), so cleaning both sides is required.

**Title words** (`title_tokens`): `title_clean` split on spaces, stop-words removed. The stop-word list is deliberately short (a, the, of, is, for, with...); "deep", "model" and "learning" are kept. When nothing is left (a title made of stop-words only), the words are kept. `--keep-stop-words` keeps them all.

**Author names** (`authors_last`, `first_author_last`): only the **surname** is kept, since given names are written very differently on the two sides:

- BibTeX almost always writes "Surname, Given name" (`Vaswani, Ashish`), Semantic Scholar "Given name Surname" (`Ashish Vaswani`), and 40% have initials only (`J. Devlin`).
- With a comma, the surname is the part before the comma; without one, it is the last word. Both give `vaswani`.
- The **last word of the surname** is used, so that both sides agree:
  - `van Beethoven, Ludwig` and `Ludwig van Beethoven` both give `beethoven`;
  - `Garcia-Perez, J.` and `J. Garcia-Perez` both give `perez`.
- The suffixes `Jr.`, `III` are removed, and so are `others` and `et al.` (not persons).
- Organisations (names with collaboration, consortium, team...) keep their full name: `ligo scientific collaboration`.
- A BibTeX string holding several persons (`E. Özkan, H. Aydın, R. Dikici`, not split by M2) is split.

Limits, deliberately not guessed:
- Asian names in reversed order without a comma (`Wang Rui-Bing` against `Rui-Bing Wang`) give different surnames.
- Names made of initials only (`A. J`) give `j`.

Member 3 should therefore also use the overlap of the whole set of surnames, not only the first author.

**Year**:
- On the entry side, the `year` column.
- On the reference side there are two years: `year` = the year of the arXiv v1 submission, `year_s2` = the year recorded by Semantic Scholar. They differ for 1,139 of 92,916 references (1.2%), usually because the preprint and the journal version appeared in different years. Year comparisons should allow ±1 and take the smaller of the two gaps.

**arXiv ID in the entry** (`arxiv_id_in_entry`): searched in `url` → `journal` → `doi` → `title`, in that order.

- A new-style ID (`2406.05335`) is taken only when the string mentions arXiv or CoRR (an `arxiv.org` link, `arXiv:`, the DOI `10.48550/arXiv.`, `CoRR abs/...`). Page or report numbers such as `1234.5678` are thus not mistaken for IDs.
- Validity check: month 01–12, from 04/2007 on, a 4-digit number before 2015 and a 5-digit number from 2015.
- An old-style ID (`hep-th/9901001`) is taken anywhere, since the archive name makes it unambiguous.
- Normalisation: lower case, `v2` removed, the subject class of old IDs removed (`math.AG/0101001` → `math/0101001`, the form used by arXiv and Semantic Scholar).

**DOI**: lower case, `https://doi.org/`, `doi:` and trailing punctuation removed. A paper can have two DOIs, one from the journal and one from arXiv (`10.48550/arxiv.<id>`). 21,988 references on the Semantic Scholar side have only the arXiv DOI, so a DOI comparison must also compare `10.48550/arxiv.<ref_arxiv_id>`.

### 3. Candidate pairs (`candidates.py`)

- A publication with m entries and n references gives m×n rows `(arxiv_id, citation_key, ref_arxiv_id)`: exactly the candidate matrix of the statement. Pairs are formed inside one publication only.
- A publication with n = 0 (no arXiv reference at all) **has no row**: every one of its entries has no match.
- The rows are written in blocks of 500,000, so the 6 million pairs never need to be in memory at once.
- `paper_stats.csv`, one row per publication:
  - `m`, `n`, `pairs`;
  - `entries_with_arxiv_id`, `arxiv_id_hits`: the entries that write an arXiv ID, and how many of those IDs are among the publication's references;
  - `entries_with_doi`, `doi_hits`: the entries with a DOI, and how many of those DOIs match a reference;
  - `entries_without_title`, `misc_entries`.

  Member 2 uses this file to choose the publications to label by hand: m ≥ 20 (the statement asks for ≥ 20 entries), n > 0, few entries without a title.

### 4. Split (`split.py`)

- The split is **by publication**, not by entry, so that a publication is never in both train and test.
- Statement: "a group of k members puts k manually labeled and k automatically matched publications into each of the two sets". For a group of 5: **test has 5 manual + 5 automatic publications, valid also 5 + 5**, and the rest is train. The task assignment said 1 + 1; the split follows the statement (`--members 5`). With `--per-range` (used for the final split, see `docs/m3_labeling.md`), each member's ID range gives exactly 1 manual + 1 automatic publication to test and 1 + 1 to valid.
- It reads Member 2's label file (CSV or Parquet, with the columns `arxiv_id` and `source` = `manual`/`auto`). A publication with at least one manual label counts as manual.
- A publication with n = 0 is never drawn for test/valid (no candidate to rank); it stays in train. This needs `--stats`.
- The same `--seed` gives the same result. Too few publications raises an error (at least 10 of each kind are needed).

### 5. Comparing two data versions (`snapshot_diff.py`)

When M1 or M2 runs again, `citation_key` and the references can change, while the labels are tied to the keys. Usage:

1. Run `m3_data.py` for the old and the new version into two folders.
2. Run `snapshot_diff.py` on the two folders. It lists, per publication, the entries and references that were **removed**, **added**, or **changed** (naming the columns that changed).
3. With `--labels`, it also writes `diff_labels.csv`: the labels to review, because their entry changed or disappeared, or the labelled reference no longer exists.

### 6. Pipeline (`pipeline_m3.py`)

Convention for the later steps (Members 3, 4): every script runs as

```
python src/m3/<script>.py --work WORK
```

and reads and writes its files in `WORK` (`features.parquet`, the models, `predictions`...). The pipeline calls `features.py`, `modeling/train.py`, `modeling/predict.py` and `modeling/evaluate.py` in order, when the files are present.

## Trial run on Bao's range

Input:
- the `bibtex` table of the frozen M2 version (tag `m2-final`, in the folder `g11_m2_benchmark`);
- the references of the M1 crawl of 1 November (`MS2_input/GBao_MS2.tar.gz`). The frozen M2 version read a newer crawl, so these numbers are for the trial only.

| | |
|---|---|
| Entries | 223,233, in 4,709 publications |
| References with an arXiv ID | 92,916, in 4,338 publications |
| Publications with both entries and references | 4,130 |
| Publications with entries but no reference (every entry has no match) | 579 |
| Candidate pairs | 5,831,166 (per publication: median 646, median m 40, median n 17) |
| Publications with m ≥ 20 and n > 0 (enough for manual labelling) | 3,531 |

The simplest rules on the cleaned data (a hint for Member 2, not labels) already link 39.3% of the entries:
- same arXiv ID: 33,345 entries;
- no common ID but the same DOI (arXiv DOI included): 16,148 entries;
- no common ID or DOI but an identical `title_clean`: 38,283 entries.

The rest have a similar title, or truly have no answer.

## Data errors found (reported only, not fixed)

`m3_data.py` prints `!!` lines on every run. On Bao's range:

- **`bibtex` table (M2):**
  - 10,400 author names end with a stray `\` (`Yao, D.-X. \`); the cleaning removes it.
  - 18 author strings hold several persons (`E. Özkan, H. Aydın, R. Dikici`); the cleaning splits them.
  - 6 names lost their accents in M2 (`Ir {s}i {c}, V.`, originally `Iršič`); the surname comes out wrong and cannot be recovered.
  - 6,857 entries have no title (3.1%), 1,633 entries have no author, 2,289 entries have no year.
- **References (M1, Semantic Scholar):**
  - Incomplete reference lists: 579 publications have entries but no arXiv reference at all. Some entries write an arXiv ID that is not in the publication's reference list.

    Matching cannot recover these cases; making up for them would mean adding data to the M1 `references` table, which is the group's decision.
  - The arXiv v1 year and the Semantic Scholar year differ for 1,139 references.

## For the other members

- **Member 2 (labelling):**
  - Use `paper_stats.csv` to choose the publications.
  - The automatic rules read `arxiv_id_in_entry`, `doi`, `title_clean`, `year` and `first_author_last` in both tables. Remember to compare the arXiv DOI as in section 2.
  - The manual labelling tool shows `title`, `authors`, `url` (the original columns).
- **Member 3 (features):**
  - Compute on `candidates.parquet`, joined with the two cleaned tables by key.
  - `title_tokens` for Jaccard/TF-IDF, `title_clean` for Levenshtein and the token-set ratio.
  - `authors_last` for the surname Jaccard; `year` and `year_s2` for the year gap.
- **Member 4:** `splits.json` uses the publication keys as they are; the `predictions` table takes `citation_key` from `entries_clean.parquet`.
- **Member 5:** the `references` table on Hugging Face takes exactly the original columns of `references_clean.parquet` (`arxiv_id, ref_arxiv_id, ref_title, ref_authors, ref_submission_date, ref_s2_id`). The cleaned columns are for internal use only.
