# M3 – Features (feature engineering)

Input: `labels.csv` (see `docs/m3_labeling.md`), with `entries_clean.parquet` and `references_clean.parquet` (see `docs/m3_data_candidates.md`). The analysis with figures is in the notebook `src/m3/notebooks/m3_features_model.ipynb`.

```bash
python src/m3/features.py --work data/m3_all                       # -> features.parquet, features.json
python src/m3/feature_report.py --work data/m3_all --part train    # -> feature_report_train.csv
python src/tests/m3/test_features.py
```

## 1. Framing: learning to rank

Each BibTeX entry is a **group**. Its candidates are the references with an arXiv ID of **the same publication**, that is, one row of the m×n matrix of the statement.
- The model scores each (entry, candidate) pair, sorts the candidates and keeps the top 5. This matches MRR@5 directly.
- The features are computed **per pair**, so the same table serves both the ranking model (LightGBM `lambdarank`, the main model) and a pairwise classifier (Logistic Regression, a baseline).

**Samples.** As the lecturer answered, a label is **a matched pair**. Only entries with `is_match = true` are therefore samples. Each sample gives a group of 1 pair with `y = 1` (the correct reference) and n − 1 pairs with `y = 0`. "No match" entries still have rows (`is_sample = 0`), but only so that the model ranks their candidates for the predictions table; they are not used for training or scoring, and are **never** treated as negatives.

Size of `features.parquet`: 4,636,680 pairs, 126,065 entries, of which 63,612 are samples.

## 2. The features (31, in 6 groups)

Notation: E is the entry, R the candidate reference. Titles are cleaned: LaTeX removed, accents removed, lower case, punctuation removed. Authors are compared by **surname**. NaN means the value cannot be computed, for example when a title is missing.

| Group | Feature | Computation | Idea |
|---|---|---|---|
| link | `id_match` | the arXiv ID written in E (url/journal/doi/title) = the ID of R | near-certain evidence, but only about 37% of the entries carry an ID |
| | `doi_match` | the DOI of E = the DOI of R, or = `10.48550/arxiv.<ID>` | the same, for DOIs |
| | `entry_has_id`, `entry_has_doi` | whether E writes an ID / a DOI | tells the model whether "no ID match" means a missing ID or a different paper |
| | `entry_id_in_paper`, `entry_doi_in_paper` | whether the ID / DOI of E matches any reference of the publication | *dropped: duplicates `entry_has_*`* |
| title | `title_jacc3` | Jaccard of the character 3-grams of the title without spaces | robust to joined/split words (`M 2 diffuser` = `M2Diffuser`); a few changed letters cost little |
| | `title_jacc_word` | Jaccard of the word sets (stop-words removed) | simple; sensitive to how words are split |
| | `title_tfidf` | TF-IDF cosine; the IDF is computed from every title of the group's corpus | rare words weigh more than common ones |
| | `title_exact`, `title_contain` | identical titles; one title contains the other | clear signals; catch truncated titles or added subtitles |
| | `title_len_ratio` | shorter length / longer length | separates "X" from "Improved X" |
| | `entry_title_missing` | E has no title | tells the model to rely on authors and year |
| author | `first_author_match` | the first-author surname of E is among the surnames of R | citations usually keep the first author right |
| | `author_jacc`, `author_overlap` | Jaccard and number of shared surnames | separates papers with the same title but different authors |
| | `entry_authors_missing`, `entry_authors_group` | E has no authors; the authors are only "et al." or an organisation | then no surname match is normal |
| year | `year_gap` | the smallest \|year of E − year of R\| (R has an arXiv year and an S2 year) | the right paper is usually 0–1 year apart (preprint against published version) |
| | `year_gap_signed` | the same, with its sign | *dropped: right and wrong pairs have the same mean* |
| | `year_missing` | the year is missing | flag |
| venue | `venue_sim` | 3-gram Jaccard between the journal/booktitle of E and the venue of R | secondary signal |
| | `entry_is_preprint` | E is an arXiv preprint | *dropped: duplicates `entry_has_id`* |
| | `entry_type_code` | code of `entry_type` | book/misc entries are rarely on arXiv |
| context | `n_candidates` | number of candidates of the publication | |
| | `title_rank`, `tfidf_rank`, `author_rank` | the rank of R among the candidates of E, for each similarity | a ranking problem needs **relative** comparisons: 0.6 can be the best in one publication and poor in another |
| | `title_gap_best`, `title_margin_second` | the distance to the best candidate; the lead of the best candidate over the second | how certain the title evidence is |
| | `title_is_best` | R has the most similar title | *dropped: duplicates `title_rank = 1`* |

**No leakage.** No feature comes from the labels, the names of the labelling rules (`note`) or `rule_hint`. The "context" features only compare R with the **other candidates of the same entry**, which are also all available at prediction time. **External knowledge:** none. No outside model or corpus is downloaded; TF-IDF is fitted on the titles of the group's own corpus (as the lecturer answered, this does not count as external).

## 3. Analysis on train (`feature_report_train.csv`)

Computed on the 62,866 samples of the training set, without looking at test.

| Feature | yes / no | MRR@5 alone |
|---|---|---|
| title_jacc3 / title_jacc_word / title_tfidf | 0.99 / 0.05–0.08 | 0.993 / 0.993 / 0.992 |
| title_exact / title_contain | 0.95–0.97 / 0.00 | 0.950 / 0.954 |
| author_jacc / author_overlap | 0.92 / 0.03 · 5.7 / 0.3 | 0.944 / 0.894 |
| first_author_match | 0.99 / 0.06 | 0.597 |
| id_match / doi_match | 0.38 / 0.00 · 0.33 / 0.00 | 0.376 / 0.336 |
| year_gap (years) | 0.64 / 4.9 | 0.225 |
| venue_sim | 0.40 / 0.09 (20% missing) | 0.253 |
| title_rank, title_gap_best, tfidf_rank, author_rank | | 0.992–0.993; author_rank 0.944 |

- "yes" / "no" are the means over right / wrong pairs. For a 0/1 feature, the mean is the rate.
- Entry-level features (missing flags, `entry_has_*`, `entry_type_code`, `n_candidates`) are the same for every candidate of an entry, so their MRR alone is ≈ 0; they only help in combination with other features.
- **Correlations > 0.9:**
  - `entry_has_id` ≈ `entry_id_in_paper` (1.00) ≈ `entry_is_preprint` (0.98);
  - `entry_has_doi` ≈ `entry_doi_in_paper` (0.94);
  - the three title similarities (0.92–0.96);
  - `title_exact` ≈ `title_contain` ≈ `title_is_best` ≈ `title_margin_second` (0.95–0.98).

## 4. Feature selection: 31 → 26 → 17

1. **5 redundant or useless features dropped** (section 3): `entry_id_in_paper`, `entry_doi_in_paper`, `entry_is_preprint`, `year_gap_signed`, `title_is_best`. **26** features remain, the "full" set.
2. **Group ablation and permutation importance** (`src/m3/modeling/ablation.py`, 5-fold cross-validation by publication on train; see `docs/m3_model.md` for why valid is not used). The main measure is MRR@5 on the **3,119 hard cases** (the right pair does not have an identical title) with the **link features masked**. The full model reaches 0.9851.

| Group removed | MRR@5 | drop |
|---|---|---|
| title + context (both carry the title information) | 0.9548 | **−0.030** |
| year | 0.9778 | −0.007 |
| author | 0.9809 | −0.004 |
| venue | 0.9842 | −0.001 |
| title only / context only / link | ≈ 0.985 | ≈ 0 (the other group compensates; the link features are masked) |

Permutation importance (link masked, hard cases) ranks first: `year_gap`, `author_rank`, `venue_sim`, `author_jacc`, `first_author_match`, `title_gap_best`. The individual title features have importance ≈ 0, because they compensate for one another.

3. **The "compact" set (17 features)**, used by the final model:
   - **link:** `id_match`, `doi_match`, `entry_has_id`, `entry_has_doi`
   - **title:** `title_jacc3`, `title_tfidf`, `entry_title_missing`
   - **author:** `first_author_match`, `author_jacc`, `author_overlap`, `entry_authors_missing`, `entry_authors_group`
   - **year:** `year_gap`, `year_missing`
   - **venue:** `venue_sim`
   - **context:** `title_gap_best`, `author_rank`

   Cross-validation shows the compact set equal to the full one: hard cases without link 0.9849 against 0.9851, less than 1 entry out of 3,119. With 9 features fewer and easier to explain, the compact set was chosen.

The lists are in `src/m3/modeling/pairs.py` (`DROPPED`, `COMPACT`). `features.parquet` still keeps all 31 columns.

## 5. Summary

- **The title is the strongest signal:** on its own it gives an MRR of about 0.99 on train.
- **Authors and year settle** papers with the same or nearly the same title, for example "Generative adversarial networks" by Goodfellow and the review of the same title by Cohen; DDPM and Improved DDPM.
- **The link features are almost never wrong** but exist for only about 1/3 of the entries. During training they are therefore masked for 50% of the entries, so that the model does not depend on them (see `docs/m3_model.md`).
