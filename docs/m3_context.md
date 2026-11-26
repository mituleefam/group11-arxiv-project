# M3 – Experiment: predicting the reference from the citation context

The main model of M3 matches a BibTeX entry with its reference using the entry's own metadata (title, authors, year, arXiv id, DOI). With that, the task is easy: MRR@5 is close to 1, and author + year + venue alone already reach 0.95 on the hard entries.

This experiment asks a harder question. Using **only the text that cites the entry**, without reading any field of the entry, can we tell which reference it is? The text is the sentence holding its `\cite`, the sentences around it, the heading path and the figure caption, all taken from the M2 hierarchy. The code is in `src/m3/context/`, the outputs are in `WORK/context/`, and the analysis is in `src/m3/context/notebooks/m3_context_model.ipynb`. The main code and notebook of M3 are unchanged.

## Running

```bash
python src/m3/context/contexts.py     --tables M2_TABLES --out WORK/context   # ~2.5 min for 23 000 papers
python src/m3/context/corpus_links.py --work WORK                            # ~1 min
python src/m3/context/ctx_features.py --work WORK                            # ~9 min
python src/m3/context/analyze_ctx.py  --work WORK                            # ~25 min (CV on train)
python src/m3/context/train_ctx.py    --work WORK --ctx-set compact          # CV on train
python src/m3/context/train_ctx.py    --work WORK --ctx-set full --test      # CV + test, scored once
python src/m3/context/select_meta_ctx.py --work WORK                         # ~9 min: features of the combined model
```

`M2_TABLES` is the `tables/` folder of `pipeline_m2.py` (`elements/`, `hierarchy/`). `WORK` is the M3 working folder (`features.parquet`, `references_clean.parquet`, `entries_clean.parquet`, `labels.csv`, `splits.json`). The scripts use pyarrow, pandas, numpy, scipy, scikit-learn and LightGBM, the same libraries as the main model.

## Steps

| Script | Output | What it does |
|---|---|---|
| `contexts.py` | `mentions.parquet` | One row per citation (citing element × key). Columns: `left` / `right` (the words just before / after the citation, stopped at another citation), `sentence` (±60 words; for a figure, its caption), `neighbours` (previous and next sentence under the same parent), `headings`, `section` (type of the top-level heading), `n_keys`, `position`. |
| `corpus_links.py` | `links.parquet` | Sure entry → arXiv links in **every** paper of the corpus, using only rules R1–R3 of `auto_label.py` (same id, same DOI, same title, with its checks). No R4, no model, no labels. |
| `ctx_features.py` | `features.parquet`, `splits.json`, `features_ctx.json` | The rows and columns of `WORK/features.parquet`, plus 27 context features. `modeling/pairs.Pairs(WORK/context)` reads this folder like `WORK`. |
| `analyze_ctx.py` | `ctx_feature_report.csv`, `ctx_ablation_cv.csv`, `ctx_importance_cv.csv` | Each feature alone, group ablation and permutation importance, all by CV on train. |
| `train_ctx.py` | `ctx_cv_report_<set>.json`, `oof_scores_<set>.parquet`, `ctx_test_report.json` | Compares the scorers by CV on train; with `--test`, fits them on train and scores test once. |
| `select_meta_ctx.py` | `select_meta_ctx.json` | Chooses by CV how many context features the combined model adds to the 17 metadata features (0, 4, 13 or 27). It then scores `meta` and the chosen set on train (CV), valid and test. |

**Masking.** A key such as `zheng2024large` gives away the author and year. Every citation of the entry therefore becomes the word `zzcitezz`, any other citation becomes `zzotherzz`, and keys never reach the text. Maths becomes `zzmathzz`. `\ref`, `\label`, URLs and formatting commands are dropped. The text is lower-case ASCII (`text_clean.normalize_text`).

**Describing the candidates from the corpus.** A candidate's title is too short to compare with a sentence, and abstracts would need outside data. So each candidate is also described by what **other** papers of the corpus write when they cite it: the contexts of every entry linked to it in `links.parquet`. The paper being scored is always taken out: its own linked contexts are subtracted from the description, so a paper never describes its own candidates. As a check, the number of describing papers has the same distribution for the right and the wrong candidates.

**Text models.** TF-IDF (unigrams and bigrams, sublinear tf, 400 000 terms) and LSA (200 dimensions) are fitted on the contexts of the whole corpus and the candidate titles. This is the group's own data; no pretrained model is used.

## Features (`ctx_features.CTX_GROUPS`)

| Group | Features |
|---|---|
| `ctx_title` | TF-IDF cosine of local / sentences / all context with the candidate title; LSA cosine; rank and gap to the best candidate of the entry |
| `ctx_desc` | The same with the candidate's description; `n_desc` = log(1 + other papers that describe it); `desc_missing` |
| `ctx_author` | The candidate's first-author surname in the local words / in the context; number of its surnames in the context |
| `ctx_year` | Candidate age (paper year − candidate year) and its rank |
| `ctx_entry` | The same for every candidate of the entry: log(1 + mentions), mean keys per citing element, first position, section, share in figures, `ctx_missing`, number of candidates |

`CTX_COMPACT` (13 features, chosen by permutation importance) is the alternative set.

## Scorers

| Name | What it is |
|---|---|
| `random` | A random order (the floor) |
| `tfidf_title`, `tfidf_desc` | TF-IDF cosine of the citing sentences with the title / the description |
| `ctx` | LightGBM lambdarank (the main model's parameters) on the context features only |
| `ctx+assign` | `ctx` plus a one-to-one assignment per publication: two entries do not cite the same reference. It uses the Hungarian algorithm on the scores of all entries of the paper, labelled or not. Each entry may also take "no candidate" at a score θ, chosen on the out-of-fold scores. The assigned candidate is ranked first. |
| `meta` | The main model's configuration: 17 compact metadata features, link mask share 0.5 |
| `meta+ctx`, `meta+ctx+assign` | Metadata plus context features (and the assignment) |

The labels, split, folds (by publication, seed 11) and metric (MRR@5, pessimistic ties) are those of the main model. "With context" leaves out the 0.9% of labelled entries that are never cited in the text: the key is only in the `.bib`, appears in `\nocite`, or sits in a commented-out line. "Hard" means the labelled pair does not have an identical title.

## Results

Cross-validation on train (5 folds, 62 866 labelled entries, 45 candidates per entry on average), full set:

| Scorer | MRR@5 (with context) | MRR@5 hard | Hit@1 |
|---|---|---|---|
| random | 0.088 | 0.089 | 0.038 |
| tfidf_desc | 0.264 | 0.162 | 0.191 |
| tfidf_title | 0.323 | 0.270 | 0.225 |
| ctx | 0.442 | 0.356 | 0.334 |
| ctx+assign | **0.459** | **0.361** | **0.359** |
| meta (no link) | 0.9993 | 0.9849 | 0.9989 |
| meta+ctx (no link) | 0.9994 | 0.9875 | 0.9990 |
| meta+ctx+assign (no link) | 0.9996 | **0.9927** | 0.9994 |

Group ablation of `ctx` (MRR@5 drop): `ctx_title` −0.101, `ctx_desc` −0.061, `ctx_author` −0.012, `ctx_year` ≈ 0, `ctx_entry` −0.003. In `meta+ctx`, removing any single context group changes the hard no-link score by at most ±0.001, and removing all of them costs 0.0026. The compact set is 0.003 below the full set on `ctx+assign`, so the full set was chosen before test was read.

Test, scored once (346 labelled entries from 10 papers; 338 with context, 29 hard):

| Scorer | MRR@5 (with context) | MRR@5 hard | Hit@1 |
|---|---|---|---|
| random | 0.059 | 0.086 | 0.027 |
| tfidf_title | 0.310 | 0.320 | 0.207 |
| ctx | 0.395 | 0.386 | 0.287 |
| ctx+assign | 0.392 | 0.435 | 0.287 |
| meta (no link) | 0.9985 | 0.9828 | 0.9970 |
| meta+ctx (no link) | 1.0000 | 1.0000 | 1.0000 |

The test set is small: the standard error of MRR@5 is about ±0.02 overall and ±0.08 on the 29 hard entries. The order of the scorers is the same as in CV.

## Features of the combined model (`select_meta_ctx.py`)

The 17 metadata features stay fixed: they are the main model's choice, so that `meta` is exactly the main model. What is chosen, by CV on train with the same folds, is how many context features to add:
- 4: the largest permutation importances, `sim_local_title`, `sim_local_desc`, `n_desc`, `fa_in_local`;
- 13: `CTX_COMPACT`;
- 27: every context feature.

Each set is measured with and without the one-to-one assignment. The key, as for the main model, is the mean MRR@5 on all, all with no link and hard with no link (with the assignment). The smallest set within 0.0005 of the best is chosen.

| Set | Features | Key | Hard, no link | Hard, no link, +1-1 |
|---|---|---|---|---|
| meta | 17 | 0.99657 | 0.9849 | 0.9902 |
| meta+ctx4 | 21 | 0.99717 | 0.9865 | 0.9919 |
| meta+ctx13 | 30 | 0.99750 | 0.9880 | 0.9929 |
| meta+ctx27 | 44 | 0.99743 | 0.9875 | 0.9927 |

The chosen set is **meta+ctx4 (21 features)**: only 0.0003 below the best, with 9 fewer features. Adding all 27 is no better than adding 13.

Gain over `meta`, MRR@5 with no link (`meta` → `meta` + 1-1 → `meta+ctx4` + 1-1):

| Part | Entries (hard) | All | Hard |
|---|---|---|---|
| train (CV) | 62 866 (3 119) | 0.9992 → 0.9995 → 0.9996 | 0.9849 → 0.9902 → **0.9919** (error −46%) |
| valid | 400 (22) | 1.0000 → 1.0000 → 1.0000 | 1.0000 → 1.0000 → 1.0000 |
| test | 346 (29) | 0.9986 → 1.0000 → 1.0000 | 0.9828 → 1.0000 → 1.0000 |

In CV, the assignment brings +0.0053 on the hard entries with no link and the context features another +0.0017. Valid is too easy to tell the models apart. On test the assignment alone already corrects the few errors, so the part due to the context cannot be separated there.

## Leak check: strict descriptions

Describing candidates with other papers' contexts has two risks. A paper submitted **after** the one being scored could describe its candidates, and the **same authors** could reuse nearly the same sentence. `ctx_features.py --strict --m1 "M1_DIR/corpus/*/metadata.json" --out WORK/context_strict` rebuilds the descriptions using only papers submitted **before** the scored paper (M1 submission date) that share **no author** with it. The same CV is then rerun.

| CV on train | Usual description | Strict description | Difference |
|---|---|---|---|
| tfidf_desc (with context) | 0.2642 | 0.2069 | −0.057 |
| ctx (with context) | 0.4416 | 0.4261 | −0.016 |
| ctx+assign (with context) | 0.4586 | 0.4402 | −0.018 |
| meta+ctx4 +1-1, hard no link | 0.9919 | 0.9919 | 0.000 |
| meta+ctx13 +1-1, hard no link | 0.9929 | 0.9931 | +0.0002 |

- **The combined model is unchanged**, and the chosen set is still +4. The conclusions on the combined model do not depend on these risks.
- **The context-only model loses 0.018.** Part of this is that strict descriptions are much rarer: the right candidate has no description for 56% of entries instead of 40%, because only about half the papers are earlier. The rest is what later papers or the same authors brought. The two parts cannot be separated, so **0.44** (strict) is the conservative figure to report for the context-only model.
- The descriptions still help in strict mode: `ctx` is 0.426, against 0.380 with the description group removed.

## Findings

- Without the entry's metadata the task is hard. From the context alone the model reaches MRR@5 0.46 in CV (0.44 with strict descriptions), with the right reference first for 36% of the entries, against 4% at random.
- The strongest signals are the **words right next to the citation** compared with the candidate title, and the **candidate's description by the other papers of the corpus**. Both come entirely from the group's own data.
- The one-to-one assignment adds about 0.02 in CV.
- The context-only model uses all 27 features (the 13-feature set is 0.003 lower). The combined model only needs **4** context features on top of the 17 metadata features; adding more does not help.
- Added to the main model, the 4 context features plus the assignment **cut the error on the hard entries with no identifier by 46%** (CV 0.9849 → 0.9919, mostly from the assignment; test 0.983 → 1.000; valid 1.000 for both).

## Limitations

- Contexts are thin: a reference is cited in one sentence at the median, and 42% of citing sentences cite several references.
- 40% of the right candidates have no description from other papers, only their title.
- No abstracts (outside data: to be cleared with the lecturer) and no pretrained embeddings; both would likely help the context-only model.
- Ten test papers are few for a hard task. The CV on the 2 515 train papers is the more reliable number.
