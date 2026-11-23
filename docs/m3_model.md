# M3 – Model, evaluation and the predictions table

The code is in `src/m3/modeling/`. The analysis with figures is in the notebook `src/m3/notebooks/m3_features_model.ipynb`. The features are described in `docs/m3_features.md`.

```bash
python src/m3/modeling/train.py    --work data/m3_all                 # choose the model (CV on train), train the final model
python src/m3/modeling/ablation.py --work data/m3_all                 # group ablation + permutation importance (CV)
python src/m3/modeling/evaluate.py --work data/m3_all                 # MRR@5 on valid
python src/m3/modeling/evaluate.py --work data/m3_all --part test     # test: run ONCE only, at the end
python src/m3/modeling/predict.py  --work data/m3_all                 # predictions table
python src/m3/modeling/errors.py   --work data/m3_all                 # error analysis
python src/tests/m3/test_modeling.py
```

`pipeline_m3.py` calls `modeling/train.py`, `predict.py` and `evaluate.py` in that order. It needs `numpy`, `scikit-learn`, `lightgbm` (and `pandas`, `matplotlib` for the notebook), all in `src/requirements.txt`. No outside API is called and no pretrained model is used.

| File | Role |
|---|---|
| `pairs.py` | reads `features.parquet` + `splits.json`; the feature sets (`DROPPED`, `COMPACT`); link masking; MRR@5, Hit@1, Hit@5 |
| `models.py` | 2 baselines, Logistic Regression, LightGBM; saving/loading models |
| `train.py` | 5-fold cross-validation by publication on train; model choice; final model trained on the whole training set |
| `ablation.py` | group ablation and permutation importance (CV) |
| `evaluate.py` | MRR@5 on valid or test |
| `predict.py` | the `predictions` table |
| `errors.py` | error analysis; matched pairs the automatic rules may have missed |

## 1. Models compared

| Name | Kind | Description |
|---|---|---|
| `rules` | baseline | the automatic labelling rules written as a score: `4×id_match + 4×doi_match + 2×title_exact + title_jacc3` |
| `title` | baseline | title similarity only (`title_jacc3`) |
| `logreg_m00`, `logreg_m50` | pairwise classification | Logistic Regression (median imputation, standardisation, balanced classes) |
| `lgbm_m00`, `lgbm_m50` | **learning to rank** | LightGBM `lambdarank`, one group per entry, 300 trees, learning rate 0.05, no early stopping |

- **`_m00` / `_m50`:** the share of entries whose **link features are masked** during training. Masking sets `id_match`, `doi_match`, `entry_has_id`, `entry_has_doi` to 0, as if the entry wrote no ID/DOI. The aim is to force the model to match by title, authors and year (against "shortcut learning" through the ID).
- **Training:** on the samples (`is_sample = 1`) of the training set only. Each group has 1 right pair and n − 1 wrong pairs.

## 2. Model choice: why cross-validation on train

The split follows the requirement: in valid and test, each member contributes 1 manual + 1 automatic publication (`split.py --per-range`). But **valid is too easy**: on its 400 entries **every** scorer (the "title only" baseline included) reaches MRR@5 = 1.000, so it cannot tell the models apart.

The models are therefore compared by **5-fold cross-validation on train**:
- 62,866 entries, split **by publication**, so a publication is never both in the training part and in the scored part;
- valid and test are **not** used at this step.

Valid is reported as a sanity check. Test is scored **once**, after the features and the model are fixed.

**Three numbers are tracked:**
- **all:** plain MRR@5. Close to 1 for every model, because most training labels are produced by the rules themselves (circular).
- **no link:** with the link features masked (an entry that writes no ID/DOI).
- **hard, no link:** on the **3,119 hard entries** (the right pair does not have an identical title) with the link features masked. This is the most honest number.

The chosen model has the highest mean of these three numbers.

| Model (full set, 26 features) | all | no link | **hard, no link** |
|---|---|---|---|
| rules | 1.0000 | 0.9934 | 0.8670 |
| title | 0.9934 | 0.9934 | 0.8670 |
| logreg_m00 | 1.0000 | 0.9986 | 0.9719 |
| logreg_m50 | 1.0000 | 0.9990 | 0.9800 |
| lgbm_m00 | 1.0000 | 0.9990 | 0.9795 |
| **lgbm_m50** | 0.9999 | 0.9993 | **0.9851** |

With the compact set (17 features), `lgbm_m50` reaches 0.9999 / 0.9992 / 0.9849, almost the same as the full set, so **the final model is `lgbm_m50` on the compact set**. Model file: `WORK/models/lgbm_m50.pkl`; CV results: `WORK/train_report.json` (compact) and `train_report_full.json` (full).

## 3. Results

**Valid** (400 entries): every scorer reaches 1.000.

**Test** (scored once; 346 entries: 138 manual, 208 automatic):

| | MRR@5 all | manual | automatic | no link: all | no link: manual | Hit@1 manual |
|---|---|---|---|---|---|---|
| rules | 0.9986 | 0.9964 | 1.0000 | 0.9908 | 0.9843 | 0.9928 |
| title | 0.9908 | 0.9843 | 0.9952 | 0.9908 | 0.9843 | 0.9783 |
| **lgbm_m50 (chosen model)** | **1.0000** | **1.0000** | **1.0000** | 0.9986 | 0.9964 | 1.0000 |

## 4. Error analysis (`errors.py` → `errors.csv`, `errors_summary.json`)

| Source | Entries | Title baseline wrong | Model wrong | Fixed by the model | Broken by the model |
|---|---|---|---|---|---|
| test, as is | 346 | 4 | **0** | 4 | 0 |
| test, no link | 346 | 4 | 1 | 3 | 0 |
| train (out-of-fold), as is | 62,866 | 457 | **10** | 447 | 0 |
| train (out-of-fold), no link | 62,866 | 457 | **71** | 390 | 4 |

**Examples of title-baseline errors fixed by the model:**
- `CoverUp: Coverage-Guided, LLM-Based Test Generation` ↔ `CoverUp: Effective High Coverage Test Generation for Python` (a renamed paper);
- the ICSE version of ClassEval;
- an entry whose title is `"Folkestad"` (an M2 parsing error) but whose authors and year match;
- a reference whose title is stored by S2 as `OUP accepted manuscript`.

**The model's remaining errors** (no link, train) fall into 2 main kinds:
- **rewritten title** (35): the entry's title is almost unrelated to the reference's. The cause is a junk title stored by S2 (`OUP accepted manuscript`, MathML markup, `\documentclass…`), or M2 taking an author name, a journal or an arXiv ID as the title (`"arXiv:2411.17604"`, `"Abdullaev"`).
- **missing information** (32): the entry has no title. Only authors and year are left, which cannot separate papers **by the same authors in the same year**.

Most errors come from the **data**, not from the model. They could be reduced by cleaning titles in M2 (parsing errors) and in M3 (removing MathML/LaTeX from S2 titles).

## 5. Limitations: why MRR ≈ 1

1. **The automatic labels are circular.** 2,500 of the 2,525 publications have labels produced by the rules (same ID, same DOI, identical title), and the model uses those very signals. The `rules` baseline also reaches about 1.
2. **The manual publications lean towards easy ones.** To reach ≥ 20 matched pairs per publication, only publications well linked by S2 were picked.
3. **The task is easy on this data.** Each entry is compared with only about 40 references of its own publication, and authors usually cite the title almost verbatim.
4. **"No match" entries are not scored** (the statement's definition of |Q|). The hardest part, recognising that an entry is not on arXiv, is not counted.
5. **The rules miss matched pairs, mostly hard ones.** The recall of the rules is 0.987, measured on independent manual labels. Among the "no match" entries of the automatic publications, about **427–764** entries have a best candidate that looks like the same paper (title ≥ 0.5–0.8 and the same first author); in the manual publications there are only 2–4 such entries. Examples: two-word titles (`Inception transformer`, `Machine unlearning`), a year gap of 2, titles mixed with author names or years, renamed papers. These cases never become samples, so the data is biased towards easy cases.
   - **The rules are not changed**, because loosening them would bring in "similar but different" pairs (`Qwen2.5` ↔ `Qwen3`, `Langlands I` ↔ `IV`), and the manual labels contain hardly any such case to measure precision on. The list is in `WORK/missed_matches.csv`.

The MRR@5 ≈ 1 on test therefore shows that **the pipeline is correct**, not the model's real ability. **The real ability** is measured on the **hard cases without link**: baseline 0.867 against LightGBM 0.985; the number of misranked entries drops from 457 to 71.

## 6. The predictions table (`WORK/predictions.parquet`)

One row for **every** BibTeX entry of the publications used for matching (train + valid + test): 126,065 rows from 2,525 publications. Columns as in the statement:

| Column | Content |
|---|---|
| `arxiv_id` | the publication holding the entry |
| `partition` | `train` / `valid` / `test` (from `splits.json`) |
| `citation_key` | the entry |
| `groundtruth_arxiv_id` | the correct reference; null when the entry has no match (not a sample) |
| `cand_1` … `cand_5` | the 5 best candidates according to the model (cand_1 best); null when the publication has fewer than 5 candidates |

An extra `range` column (the member's ID range) lets each member filter their own part for their personal Hugging Face dataset. Drop this column when publishing if the schema must match the statement exactly.
