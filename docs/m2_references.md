# Milestone 2: Reference Extraction & BibTeX Standardization

Documentation of Member 3's work: extracting the references from `\bibitem` and
`.bib` files, standardising them as BibTeX, deduplicating them across versions and
renaming the `\cite{}` keys. The final result goes into the `bibtex` table.

## 1. Files

All of Member 3's code is in the subfolder `src/reference_parser/`:

```
src/
├── m2_contract.py, pipeline_m2.py, ...     (Member 1)
├── tests/m2/test_references.py             (Member 3, offline tests)
└── reference_parser/                       (Member 3)
    ├── __init__.py
    ├── ref_text.py
    ├── bibitem_parser.py
    ├── bibtex_converter.py
    ├── dedup_bibtex.py
    ├── citation_updater.py
    └── reference_stats.py
```

| File                    | Role                                                                                                                                                     | Function called by the pipeline |
| :---------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------ |
| `ref_text.py`         | Shared helpers: reading `{...}`/`[...]` groups, removing `%` comments, LaTeX → Unicode (`{\"a}` → ä, `\&` → &), normalising DOI / arXiv id / URL, the title comparison key | –                                |
| `bibitem_parser.py`   | Finds the references of **one version**: `\bibitem` in `thebibliography`, amsrefs `\bib{}`, `.bib` entries                                                | `parse(text, bib_files)`        |
| `bibtex_converter.py` | Entry → row of the `bibtex` table (title, authors, year, doi, journal, url, entry_type)                                                                | `convert(entries)`              |
| `dedup_bibtex.py`     | Merges duplicate references across versions, unions the fields, chooses the canonical key, builds the rename map                                       | `dedup(by_version)`             |
| `citation_updater.py` | Renames the keys in every `\cite`-like command according to the map                                                                                     | `update(text, rename)`          |
| `reference_stats.py`  | Runs the reference steps alone on the corpus and writes the logs and the numbers for the report                                                         | CLI                             |
| `tests/m2/test_references.py` | 26 offline tests (including tests that run the whole `pipeline_m2`)                                                                              | –                                |

The modules use only the Python standard library (no extra package to install).
They follow the contract of `src/m2_contract.py` (branch `quan-m2-member1`).
For the pipeline to find modules in a subfolder, two small changes were made to Member 1's files:

- `m2_contract.py`: `PACKAGES = ("", "reference_parser")` was added; `load()` looks for each module
  in `src/` first, then in `src/reference_parser/`.
- `pipeline_m2.py` (`code_version`): the hash also covers the files of `src/reference_parser/`,
  so that a change to Member 3's code makes the next run parse again.

## 2. Place in the pipeline (`pipeline_m2.py`)

```
gatherer.gather (M1)                       each version: text with \input merged, comments removed
   │
   ├─ bibitem_parser.parse(text, bib_files)   ← reads the RAW text, before thebibliography is removed
   ├─ bibtex_converter.convert(entries)
   │        (repeated for every version)
   ├─ dedup_bibtex.dedup(by_version)  →  rows (bibtex table) + rename (cite_rename_map.json)
   │
   ├─ cleaner → normalizer (M2) → citation_updater.update(text, rename)
   └─ hierarchy.build (M1) + Registry (M4)   sentences differing only by a citation key are now equal → 1 element_id
```

The output of each publication is in `corpus/<yymm-id>/parsed/bibtex.json` and
`parsed/cite_rename_map.json`; `pipeline_m2.py` collects them into the Parquet table
`tables/bibtex/`.

## 3. Extraction (`bibitem_parser.py`)

- **`\bibitem[label]{key} text`**: read inside `thebibliography` (and the
  environments `references`, `biblist`, `bibdiv`); when a `\bibitem` lies outside these
  environments, the whole text is scanned. A label with nested brackets (`[{Vaswani et~al.(2017)}]`)
  is still read correctly. Comment lines (`% \bibitem{x}`) are skipped; `\%` and `%` inside `\url{}` are kept.
- **REVTeX/natbib markup** `\bibinfo{title}{...}`, `\bibfield{author}{...}`
  is read directly as fields.
- **amsrefs** `\bib{key}{type}{author={...}, title={...}}`.
- **`.bib`**: a parser of our own, supporting nested `{...}`, `"..."`, bare numbers,
  `@string` + `#` concatenation, month names, `@comment`/`@preamble`, `crossref` inheritance.
  A broken entry is skipped (with a log line) and reading goes on with the next one.
- **Only cited entries are kept** (as BibTeX does): keys found in `\cite`,
  `\citep`, `\citet*`, `\parencite`, `\nocite`, ... With `\nocite{*}`, or a document
  without any `\cite`, everything is kept. The number of dropped entries is logged (`bib_uncited_dropped`).
- **`.bbl` files**: the lecturer answered in the Q&A that the references of a publication
  that only has a `.bbl` must not be lost. `gatherer` reads the `.bbl` on `\input{x.bbl}`, or when `\bibliography{x}`
  has no `x.bib` (looking for `<root>.bbl`, then `x.bbl`, then the only `.bbl` file of the folder).
  `bbl_converter.py` extracts `thebibliography` (put into the text to be read like `\bibitem`) and
  converts a biblatex `.bbl` (`\entry`, `\field`) back to BibTeX. When there is also a `.bib`,
  the `.bib` is used. For a corpus already crawled, run `src/fetch_bbl.py` before `pipeline_m2.py`
  (section 9). A publication whose source has no `.bbl` is still recorded (`gathering.json`,
  `logs/bbl_log.csv`, the metric `versions_references_in_missing_bbl`).
- **`.bib` fallback**: when the document declares no source, `gatherer`
  passes the `.bib` files of the folder (`g.bib_fallback`); `reference_stats.py` logs
  each use of the fallback.
- The same key with both a `\bibitem` and a `.bib` entry → merged into one (the `.bib`
  fields win, the missing ones come from the `\bibitem`). A key defined twice in
  one version → the first definition is kept (as BibTeX does).

## 4. BibTeX standardisation (`bibtex_converter.py`)

| Column           | How it is obtained                                                                                                                                                                                                                                                                                                                                                                   |
| :--------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `citation_key` | the original key (never empty)                                                                                                                                                                                                                                                                                                                                                       |
| `entry_type`   | the `.bib` type (biblatex aliases such as `online`, `report`, `thesis` are mapped to the standard types); for `\bibitem` it is guessed from keywords: Proceedings/Conference/NeurIPS... → `inproceedings`; Journal/Trans./Lett./`21(3)`... → `article`; thesis → `phdthesis`/`mastersthesis`; Technical report → `techreport`; a publisher → `book`; otherwise `misc` |
| `title`        | braces removed, accents → Unicode, surrounding punctuation removed                                                                                                                                                                                                                                                                                                                   |
| `authors`      | a list of `"Surname, Given name"`: split on `and` outside braces; supports `Last, First`, `First von Last`, `{Organisation name}`; `others`/`et al.` removed                                                                                                                                                                                                                       |
| `year`         | an integer from 1600 to next year; `.bib`: `year` or `date`; the year is never taken from the start of an arXiv ID such as `1805.02656`                                                                                                                                                                                                                                             |
| `doi`          | regex `10.\d{4,9}/...`, prefixes `https://doi.org/`, `doi:` removed, lower case                                                                                                                                                                                                                                                                                                      |
| `journal`      | `journal` / `journaltitle` / `booktitle` (papers in proceedings) / `series`; an arXiv preprint → `arXiv preprint arXiv:<id>`                                                                                                                                                                                                                                                       |
| `url`          | `https://arxiv.org/abs/<id>` (from `eprint` with or without `archivePrefix`, the DOI `10.48550/arXiv.*`, `journal`, `url`, `note`; old IDs lose their subject class: `math.AG/0101001` → `math/0101001`), so that the arXiv id is not lost, since the table has no eprint column; otherwise `url`, then `https://doi.org/<doi>`                                                     |

**A `\bibitem` is free text**, read in this order:

1. DOI, URL, arXiv id by regex (`\doi{}`, `\url{}`, `\href{}`, bare links).
2. Year: the natbib label `(2017)` → `(2017)` after the authors (APA) → the last year outside links/identifiers.
3. Splitting into the author / title / venue blocks:
   - with `\newblock` (a `.bbl` produced by BibTeX) → one part per block;
   - a title in quotes ``...'' (IEEE);
   - `Authors (2017). Title. Venue` (APA);
   - `Last, F., Last, F. Title.` / `F. Last, F. Last. Title.` / Vancouver `Smith J, Doe AB. Title.`:
     cut at the full stop that ends a sentence, **not** after name initials (`A.`, `J.-P.`) or
     common abbreviations (`et al.`, `Proc.`, `Vol.`, `Phys.`...);
   - Springer `Last, F., Last, F.: Title. Venue`: cut at the `:` right after the name initials;
   - mathematics, physics `F. Last, F. Last, Title, J. Math. 12 (2000)`: initials first, the venue is the
     capitalised phrase just before the volume number and the year;
   - astronomy `Last, F., Last, F. 2005, Nature, 435, 629`: the year right after the authors, usually no title
     (then `title` stays empty, since the source does not give one);
   - physics without a title `A. Einstein, Ann. Phys. 17, 891 (1905)` → authors + journal only.
4. Macros defined by the authors in the document (`\article{...}{...}`, `\jhep`) and the journal-name macros of
   the astronomy classes (`\apj`, `\mnras`...) are replaced by their content before reading; TeX programming macros
   (`\ifx`, `\csname`...) and the commands `\doi`, `\url` are not replaced.
5. The italic part (`\emph`, `{\em ...}`) of the venue is taken as `journal`.

A reference that cannot be read does not break the publication: it becomes a `misc` row with its key only (logged).

## 5. Deduplication (`dedup_bibtex.py`)

Two rows (of the same version or of different versions) are treated as **one reference** when:

1. **They have the same citation key**, unless they clearly conflict (different DOIs, different arXiv ids, or
   completely different titles). Example: `\bibitem{1}` in v1 and v2 pointing to two different papers → **not** merged.
2. **They have the same DOI**, **the same arXiv id**, or **the same URL** (unless the titles are completely different).
3. **They have the same title** after normalisation (lower case, accents and special characters removed; ≥ 3 words
   or ≥ 15 characters), years at most 1 apart and no different first-author surname.
4. **Their titles are nearly the same** (difflib ≥ 0.92), with the same year or the same first-author surname, and the same numbers in the title
   (`... II` differs from `... I`, `Llama 2` from `Llama 3`). Only rows sharing a rare
   title word are compared, so it stays fast with thousands of references.

The merge rules are transitive (union-find).

**Canonical key**: the key present in the most versions; on a tie, the key of the
latest version. The statement's example: `vaswani2017` (v1) + `vit_transformer` (v2, v3)
→ `vit_transformer`, map `{"vaswani2017": "vit_transformer"}`. Keys are unique
within a publication (case-insensitive); a key already used by another group
gets the suffix `_2`, `_3`.

**Field union**: each field takes the best value of the group, without losing information:
structured sources (`.bib`, amsrefs, `\bibinfo`) win over values guessed from `\bibitem`,
then the value most rows agree on, then the latest version, then the longest value.
`entry_type = misc` only when no row gives a more specific type.
Example: v1 {title, author, year} + v2 {doi} + v3 {year missing} → {title, author, year, doi}.

**Limit**: the rename map is shared by every version (the pipeline's
contract). When the same key points to two different papers in two versions, the `\cite`
of that key is **not** renamed (so that it never points to the wrong paper), and the second group gets
a suffixed key; these cases are counted in the column `key_conflicts`.

## 6. Renaming `\cite` (`citation_updater.py`)

Renames the keys in `\cite`, `\citep`, `\citet*`, `\citealp`, `\citeauthor`,
`\citeyear`, `\nocite`, `\Cite...`, `\parencite`, `\textcite`, `\autocite`,
`\footcite`, multicite `\cites[..]{a}[..]{b}`; the command, the notes
`[see][p.~3]` and the order of the keys are kept. A key duplicated by the renaming (`\cite{a,b}` where a and b are
the same paper) is kept once. Keys are looked up exactly first, then case-insensitively
when that is unambiguous. `\ref` and `\label` are never touched.

## 7. Logs

Inside `pipeline_m2.py`, the modules write through the logger `m2.references`
(warnings such as a broken `.bib` entry go to stderr). For the full logs and the numbers
of the report, run:

```bash
python src/reference_parser/reference_stats.py --data-dir data --test       # the first 5 publications
python src/reference_parser/reference_stats.py --data-dir data              # everything
python src/reference_parser/reference_stats.py --data-dir data --ids 2604.05535 --show   # every BibTeX row and the map
```

Written to `data/logs/`:

| File                       | Content                                                                                                                                                                                                                                                                                                                                                                                             |
| :------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reference_log.csv`      | 1 row / publication / run: the number of `\bibitem`s, `.bib` entries (total / used / dropped as uncited), versions using the `.bib` fallback, keys cited but not found, **rows before and after deduplication**, renamed keys, `key_conflicts`, the merges per rule (key / doi / arxiv / url / title / fuzzy title), the share with title/authors/year/doi/journal/url, `misc` rows, time, errors |
| `reference_versions.csv` | 1 row / version: gather status, `.bib` files used, whether the fallback was used, whether there is a `thebibliography`, the counts                                                                                                                                                                                                                                                                 |
| `reference_m2.log`       | the modules' warnings and notes, with the publication ID `[2604.05535]`                                                                                                                                                                                                                                                                                                                            |

At the end of a run, a summary is printed: total entries before → after deduplication (% reduction), renamed keys,
the share of fields found. Members 4 and 5 use `reference_log.csv` for the statistics
"entry counts before and after deduplication".

## 8. Tests

```bash
python src/tests/m2/test_references.py      # 26 tests, offline, no pyarrow needed
python src/tests/m2/test_gather_hierarchy.py      # Member 1's tests still pass with Member 3's modules present
```

There are tests for: LaTeX → text, DOI/arXiv, `.bib` (@string, #, crossref, comments,
filtering of uncited entries, `\nocite{*}`), the `\bibitem` styles (natbib `.bbl`,
IEEE, APA, Vancouver, physics, books), `\bibinfo`, amsrefs, the statement's 3-version example,
merging by DOI/arXiv, a key reused for another paper, nearly identical titles, priority of
structured sources, renaming every `\cite` variant, and a 2-version publication run through
`pipeline_m2.py` (sentences of v1 and v2 differing only by the key → 1 element).

Performance (measured in a sandbox): 1 version with 3,000 `.bib` entries + 300 `\bibitem`s,
and the deduplication of 4 versions (5,200 rows), take about 0.7 seconds.

## 9. Adding `.bbl` files to a corpus already crawled (`fetch_bbl.py`)

The first crawl kept only `.tex` and `.bib`. This script downloads again only the versions that have citations
but no source of references, copies just the `.bbl` file into the version folder and updates
`kept_files`, `kept_bytes` in `versions.json`. No file is overwritten.

```bash
python src/fetch_bbl.py --data-dir data --dry-run    # counts the versions, sends no request
python src/fetch_bbl.py --data-dir data
python src/pipeline_m2.py --data-dir data --force
```

It can be run again: versions that already have a `.bbl` are not selected any more, and versions whose source has no `.bbl`
(`logs/bbl_log.csv`) are asked again only with `--retry-no-bbl`.


## M2 full-fix review (2026-11)

- `gatherer.py` tells `.bbl` from `.bib`: a `.bbl` file goes through `read_bbl()`, not through the BibTeX parser of `.bib` files.
- `bbl_converter.py` also accepts amsrefs (`\bib{key}{type}{...}`) and harvard (`\harvarditem`). Unrecognised forms must be recorded for manual review, never turned into invented citation keys.
- `dedup_bibtex.py` does not use non-distinctive titles such as `(ATLAS Collaboration)`, `accessed...` or strings of numbers as grounds for merging. Titles with different model versions (`GPT-5.4` / `GPT-4 Turbo` / `GPT-3.5`) are not merged just because their keys look alike. The same citation key used for two different documents still needs manual review, since a global `\cite` rename cannot be inferred reliably.
- `bibtex_converter.py` expands some astronomy journal-name macros in the `journal` field and accepts years from 1600 to the current year + 1, so the start of an arXiv ID is not taken for a year.
- See `src/tests/m2/test_full_fixes.py` to reproduce the regression tests.
