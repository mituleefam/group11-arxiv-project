# M3 – Labelling: choosing the manual publications and the labelling sheet

The first two labelling steps run after `m3_data.py` and `candidates.py` of Member 1 (see `docs/m3_data_candidates.md`):

1. `src/m3/select_manual.py`: chooses the publications to label by hand, **5 publications per ID range**.
2. `src/m3/label_sheet.py`:
   - `export`: writes the Excel file the labeller fills in;
   - `import`: checks the filled-in file and writes the label file.

Only `pyarrow` is required. Writing `.xlsx` also needs `openpyxl` (`pip install openpyxl`); without it, the script writes CSV.

## What the lecturer settled (Q&A)

| Question | Answer | Effect on the code |
|---|---|---|
| What a label is | **One label = one matched (entry, reference) pair.** An entry without any reference is **not a label**. | A manual publication needs **≥ 20 matched pairs**, and each member **≥ 100 pairs**. Entries without a reference do not count towards these numbers. |
| MRR@5, \|Q\| | An entry without a reference is **not a valid sample**. Q contains the matched labels only. | Evaluation uses the `is_match = true` rows only. An all-"no" row is not part of \|Q\|. |
| Manual and automatic labelling | A publication labelled by hand **must not** go through the automatic labelling. | The automatic rules leave out the publications of `manual_pick.csv`. |
| Automatic labelling | A **matching algorithm fixed in advance** (rules), not the model the group trains. **The 10% counts publications**: about 500 per member, about 2,500 for the group. | `auto_label.py` is written as rules. |
| Split | **One pooled split for the group**: the numbers of manual and automatic publications follow the number of members, then everything forms one train/valid/test set. Test has 5 manual + 5 automatic publications, and so has valid. | `split.py` (Member 1) does this. |
| Candidates | Taken from the references **of the same publication**. When a publication has fewer than 5 candidates, the missing `cand_*` cells are **null**. | |
| External knowledge | Counts as external only when the run on Colab calls an outside API, or downloads an outside corpus or pretrained model. TF-IDF, Word2Vec trained on the group's corpus, and LR/RF/LightGBM are **not** external. | |
| Intermediate label format | Up to the group. Only the Parquet tables on Hugging Face are graded. | |
| Submission | One shared pipeline. Each member extracts **the data of their own ID range** and hosts it on **their own Hugging Face account**. | |

## Running

```bash
# 1. choose 5 publications for every range in the data -> WORK/manual_pick.csv
python src/m3/select_manual.py --work data/m3_work --data-dir data/m1

# 2a. write the labelling file -> WORK/label_sheet_<name>.xlsx
python src/m3/label_sheet.py export --work data/m3_work --picks data/m3_work/manual_pick.csv --labeler Bao

# (fill in the Excel file)

# 2b. check it and write the labels -> WORK/labels_manual_<name>.csv
python src/m3/label_sheet.py import --work data/m3_work --sheet data/m3_work/label_sheet_Bao.xlsx --labeler Bao

python src/tests/m3/test_manual_labeling.py
```

For the whole group, pass every M1 folder to `--data-dir`. Each member uses `export --range <range name>` to get the publications of their own range only.

## Step 1: how the publications are chosen

**Exact matching rules.** This is the simplest automatic matching: an entry points to a reference of the same publication when both have the same arXiv ID, the same DOI (the arXiv DOI `10.48550/arxiv.<id>` included), or an identical `title_clean`. From these rules:

- `rule_matched`: the number of entries the rules match.
- `sure_pairs`: the number of **distinct** references the rules reach. This is a **lower bound** on the labels the publication will give; the labeller usually finds more, the pairs the rules miss.
- `rule_share` = `rule_matched / m`: the share of entries the rules settle. The rest is what the labeller has to judge: either a pair the rules miss, or an entry with no reference at all.

**Eligible publications.** Every threshold can be changed on the command line.

| Condition | Default | Why |
|---|---|---|
| Semantic Scholar status | `success` | a reference list that can be trusted |
| number of entries m | 25 ≤ m ≤ 100 | enough entries, and not too many to read through |
| number of references with an arXiv ID n | n ≥ 25 | room for 20 matched pairs (the labels can never exceed n) |
| `sure_pairs` | ≥ 10 | half of the required labels are already certain, so a publication that cannot reach 20 labels is not picked |
| entries without a title | ≤ 2 | an entry without a title can hardly be labelled |
| `misc` entries | ≤ 30% | most `misc` entries are websites or software |

The range 2604.05535–2604.10534 has 1,142 eligible publications.

**Difficulty** is relative inside each range: the eligible publications are sorted by `rule_share` and cut into three equal parts. The third with the highest `rule_share` is *easy*, the third with the lowest is *hard*.

`sure_pairs / n` is not used as the difficulty, because on Bao's range the rules already reach almost every arXiv reference of an eligible publication: median 1.0, 10th percentile 0.93. The value is nearly the same for every publication, so it cannot tell easy from hard ones.

**Drawing.** Each range draws 5 publications: **1 easy, 2 medium, 2 hard**, at random inside each level with a fixed seed (`--seed 11`: the same command gives the same result). When all 5 are cs papers and the range has an eligible paper outside cs, the last pick is swapped for one of the same level outside cs.

Why:
- With easy publications only, the manual labels would repeat the automatic rules, and the test MRR would look better than it is.
- Mixing fields means the model is not evaluated on ML papers only.

`--exclude FILE` skips publications already used, for example to pick more.

`manual_pick.csv` has the columns `range, arxiv_id, primary_category, m, n, rule_matched, sure_pairs, rule_share, difficulty, reason`.

## Step 2: the labelling file

**Sheet `entries`.** One row per entry of the chosen publications: **every entry** of each publication, sorted by publication and then by `citation_key`.

| Column | Meaning |
|---|---|
| `arxiv_id` … `arxiv_id_in_entry` | the entry as M2 wrote it (title, authors, year, journal, url, doi) |
| `rule_hint` | the reference an exact rule links the entry to (`same DOI -> 2504.13554`); empty when no rule applies |
| **`label_ref_arxiv_id`** | **filled in by the labeller**: the correct `ref_arxiv_id`; **empty = no reference of the publication is this entry** |
| **`checked`** | **the labeller writes `x`** once the row is decided (also when it stays empty) |
| **`note`** | free note |
| `cand_1_*` … `cand_5_*` | the 5 references of the same publication most similar to the entry: ID, title, authors, year, score |

**The suggestion score** only orders the candidates; the labeller decides:
- `0.6 × title similarity` (difflib on the sorted words)
- `+ 0.3 × Jaccard of the author surnames`
- `+ 0.1 × year closeness` (same year 1, one year apart 0.5)

A reference named by the same-arXiv-ID or same-DOI rule is put first (score 1).

**Sheet `references`.** Every reference of the chosen publications, to look up when no suggestion is right.

**Sheet `README`.** Instructions for filling in the file.

**Why the `checked` column:** an empty `label_ref_arxiv_id` cell can mean "no match" as well as "not looked at yet". The `checked` column tells the two apart.

**Why the labels are not pre-filled:** the manual labels are the yardstick for the automatic rules (precision/recall) and for the model. Labels pre-filled from the rules would bias that yardstick towards the rules. `rule_hint` is only a hint, so the poster should state that the labellers saw the hints.

## Label file format

`import` writes `labels_manual_<name>.csv`, **one row per entry that was looked at** (marked `x`):

| Column | Meaning |
|---|---|
| `arxiv_id`, `citation_key` | the entry (key) |
| `label_ref_arxiv_id` | the correct reference; empty when the entry has no reference |
| `is_match` | `true` when there is a reference: **only these rows are labels** (counted in the 20/100, in \|Q\| and in training) |
| `source` | `manual` (the automatic rules write `auto`) |
| `labeler` | the labeller |
| `note` | note |

`is_match = false` rows stay in the file to record that the entry was looked at and judged to have no reference, but the later steps (features, training, MRR evaluation) **use the `is_match = true` rows only**.

`split.py` reads the columns `arxiv_id` and `source`; `snapshot_diff.py` reads `citation_key` and `label_ref_arxiv_id`. The group's labels are combined by concatenating the CSV files.

`import` refuses to write the file (exit code 1) and lists the errors when:
- a row has no `x` in `checked`;
- `label_ref_arxiv_id` is not a reference of the same publication;
- an entry appears twice;
- a publication has fewer than `--min-labelled` (default 20) **matched pairs**.

`import` also warns (and still writes the file) when:
- a labeller has fewer than 100 matched pairs;
- one reference is the label of several entries of the same publication (usually the same work cited twice, to be checked).

## Step 3: automatic labelling (`auto_label.py`)

```bash
# automatic labels -> WORK/labels_auto.csv
python src/m3/auto_label.py --work data/m3_all

# measure the rules against the manual labels (measurement only, no label written)
python src/m3/auto_label.py --work data/m3_all --evaluate data/m3_all/labels_manual_*.csv --errors data/m3_all/auto_vs_manual.csv

python src/tests/m3/test_auto_label.py
```

**Choosing the publications (automatic pool).** As the lecturer answered, the 10% counts **publications** outside the manual pool, per member: ceil(0.10 × (5000 − 5)) = **500 publications per range**, 2,500 for the group.

- The publications of `manual_pick.csv` are **never** drawn into the automatic pool.
- Only publications with Semantic Scholar status `success`, m ≥ 1 and n ≥ 5 are drawn. A publication without references cannot give a label, and an automatic publication that goes to test/valid needs candidates to rank.
- The draw is random with a fixed seed per range.
- `--fraction` takes more.

**Rules** (fixed in advance, not a model). The rules run inside one publication, in this order:

| Rule | Condition |
|---|---|
| R1 | same arXiv ID |
| R2 | same DOI, the arXiv DOI `10.48550/arxiv.<id>` included |
| R3 | `title_clean` equal to the title of exactly one reference, title ≥ 3 words, and (years at most 1 apart or the authors agree); for a title of ≤ 5 words, the authors of the two sides must not conflict |
| R4 | title similarity ≥ 0.85, ≥ 0.10 above the second most similar reference, authors agree (same first-author surname, or at least half of the surnames shared), years at most 1 apart |

**Ambiguous cases**

*(a) Rules that disagree on one entry.* When R1–R3 name two different references, the entry gets no label and `note` records `conflict`.

*(b) Checking R1/R2 labels again (`id_mismatch`).* An arXiv ID or DOI written by the authors **can be wrong**: an ID pasted from another paper, often another paper of the same group, or a DOI attached to the wrong paper. An R1/R2 label is therefore dropped (`note` records `id_mismatch …`) when:
- the entry has a **full title**, **and**
- the entry's title has similarity **< 0.15** with the reference's title, **and** one of the following holds:
  - the authors do not agree, **or**
  - another entry of the same publication matches that reference **by its title** (similarity ≥ 0.85). That other entry is then the paper, and the label goes to it.

  When such another entry exists, the threshold is raised to **< 0.30 when the authors do not agree**, because every renamed paper seen kept its authors. A case caught thanks to this: `autobio2024` ("Autobio: A benchmark for robotic biology labs", author "Team AutoBio") carries the ID of Chemistry3D (Li, Huang…), similarity 0.21.

A "full title" has ≥ 4 words and is not a fragment left by M2 from a garbled entry. Fragments left out: titles containing `arxiv`; journal references with numbers in ≤ 8 words (`Phys. A 20 3765`, `Chaos Solitons Fractals 202`); titles that are mostly name initials (`M. E. J. Newman`, `He and X. D. Ji`). An entry with such a broken title is not suspected, since its ID/DOI is still correct.

Why the threshold is only 0.15: **papers renamed between arXiv versions** keep their ID while their title changes completely, and their similarity ranges from 0.16 to 1.0. Examples: MonST3R 0.16; Emergent Misalignment 0.21 (renamed for Nature); MathVista 0.41; PlanBench 0.54 (the first arXiv version was called "LLMs Still Can't Plan"). Wrongly pasted IDs, on the other hand, have similarities of 0.07–0.21. The two ranges overlap, and the authors do not separate them either (a wrongly pasted ID usually belongs to the same group of authors). A low threshold was therefore chosen, accepting a few wrong IDs left in rather than dropping renamed papers.

*(c) Several entries pointing to one reference → all are kept, `note` records `shared`.* This is the same work cited under several keys: a duplicated .bib entry, both the preprint and the published version, or an entry garbled by M2. Each entry is a label.

The first version handled (c) differently: an R3/R4 label competing with an R1/R2 label for the same reference was dropped. The **102 cases dropped** that way were read again:

| Kind | Count |
|---|---|
| the dropped entry was right: the same work cited twice | 66 |
| the dropped entry was right: the R1/R2 entry has a title garbled by M2 | 12 |
| the dropped entry was right: the R1/R2 entry is an older, renamed arXiv version | 15 |
| the dropped entry was right, and **the R1/R2 entry was the wrong one** (pasted ID) | ~6 |
| unsure | ~4 |

The rules were therefore changed to (b) + (c).

`labels_auto.csv` has the same format as the manual labels (`source = auto`, `note` = the rule).

**Result on the group's data** (`data/m3_all`, seed 11): 2,500 publications, 124,456 entries, **62,601 labels**. Median 20 labels per publication (mean 25, maximum 300); 7 publications have no label.

| Range | Publications | Entries | Labels | R1 | R2 | R3 | R4 |
|---|---|---|---|---|---|---|---|
| Tram | 500 | 24,848 | 12,752 | 4,987 | 2,404 | 5,215 | 146 |
| Thu | 500 | 25,562 | 12,758 | 5,217 | 2,650 | 4,772 | 119 |
| Phat | 500 | 26,313 | 12,407 | 4,327 | 2,528 | 5,415 | 137 |
| Bao | 500 | 24,752 | 12,344 | 4,436 | 2,429 | 5,348 | 131 |
| Quan | 500 | 22,981 | 12,340 | 4,556 | 2,058 | 5,570 | 156 |
| **Total** | **2,500** | **124,456** | **62,601** | **23,523** | **12,069** | **26,320** | **689** |

Besides the labels of the table:
- 388 labels carry the note `shared`.
- 9 entries were left without a label because the rules disagreed (`conflict`).
- 11 R1/R2 labels were dropped as `id_mismatch`. All 11 were read by hand and all are truly wrong IDs/DOIs:
  - 5 IDs pasted from another paper: `wang2024answers…`, `yang2025scalable…`, `jiang2023llmparser`, `autobio2024`, `mastqlp` (the DOI of a dataset);
  - 6 DOIs pointing to an unrelated paper: `Efstathiou2022`, `Farrah2022`, `Pfeifle2019b`, `white1992density`, `merloni_fabian`, `golden2008vehicle`.

Compared with the previous version, all 102 entries dropped before are now labelled and the 11 wrong labels are removed; the final check below then removed 18 more wrong R3 labels: **62,528 → 62,601**.

**Final check** (read by hand; no rule was changed for a single case):

1. **A random sample of 40 labels** over everything: all 40 correct.
2. **R3 with a short title and different authors.** This group had errors: generic titles shared with an unrelated paper. Added condition: for a title of ≤ 5 words, the authors of the two sides must not conflict. Organisations as authors (`Team Qwen`, `OpenAI`) and surnames transliterated differently (`Shanskii`/`Shansky`) do not count as a conflict. 18 labels were removed, and all 18 were wrong on reading:
   - 10 "Generative adversarial networks" labels (Goodfellow) linked by mistake to the review of the same title by Cohen & Giryes (2022);
   - "Quantum phase transitions" (Sachdev's book ↔ Vojta's paper, 2 labels);
   - "Gaia Data Release 2" (Brown ↔ Katz);
   - "The fractional quantum Hall effect" (Störmer, Tsui ↔ Rao);
   - "Self-guided masked autoencoder", "Retrieval Augmented Time Series Forecasting", "Interlayer pairing in bilayer nickelates", "Avoiding long Berge-cycles": each title belongs to two different papers.
3. **R1/R2 whose title is far from the reference's** (full title, similarity < 0.3): 152 labels. The **v1 title on arXiv** of each reference was looked up (arXiv API, for this check only, not used by the rules):
   - 104 labels have an entry title matching the v1 title: renamed papers, or a broken title stored by S2 (`OUP accepted manuscript`, MathML markup, `\documentclass…`). These labels are **correct**.
   - 35 labels where M2 took the venue, an author name or the journal name as the title (`In Proceedings of the 2020 CHI Conference…`). Their ID/DOI is still **correct**.
   - **~10 labels are truly wrong**: the authors pasted the ID/DOI of another paper **of the same group**, for example `rawat_rl`, `LMSWY2`, `Ball2012`, `Fiacconi2017`, `Cao2019`, `munshi_quantifying_2021`, `papadopoulos2014handbook`, `matsumoto_algebraic_2002`, `Lahiri2019`. One more label points to an S2 junk record (`Green:2004wb` → "paper added in error by admin team"). The rules **cannot catch** this group: the authors agree and the title is completely different, exactly like a renamed paper.
   - ~3 labels unsure: `brito-etal-2025-modeling`, `RW`, `Greener2025_PNAS…`.

**Estimated quality:** about 10–13 wrong labels are known to remain among 62,601 (~0.02%). For the groups not read in full, the estimate comes from the random sample (40/40 correct). It cannot be claimed that no wrong label is left. `labels_auto.csv` should be seen as very high-precision "silver" labels, not gold labels.

Side finding for M3 (cleaning): Semantic Scholar titles still contain MathML markup (`<mml:math …>`), LaTeX (`\documentclass[12pt]{minimal}…`) and placeholder titles (`OUP accepted manuscript`). `title_clean` does not handle these yet, so such pairs have a low title similarity although they match.

**R4 title similarity** (`--sim`):
- Default `chars`: **Jaccard of the character 3-grams of the title without spaces**. Without spaces, `M 2 diffuser` and `M2Diffuser` give the same 3-grams, and a changed word only changes a few 3-grams.
- The first version was `words`: the difflib ratio of the sorted words, threshold 0.93. It missed `M 2 diffuser` (0.89), because sorting moves the pieces "m" and "2" elsewhere.

The two measures on real pairs:

| Pair | words | chars |
|---|---|---|
| `M 2 diffuser` ↔ `M2Diffuser` (same paper) | 0.89 | 1.00 |
| `Towards` ↔ `Toward Generalist…` (same paper) | 0.99 | 0.94 |
| `Embodied SAM` ↔ `EmbodiedSAM` (same paper) | 0.93 | 1.00 |
| `DDPM` ↔ `Improved DDPM` (different papers) | 0.90 | 0.83 |
| `Qwen3-TTS` ↔ `Qwen3-ASR` (different papers) | 0.85 | 0.62 |
| `V2V4Real` ↔ `V2X-Real` (different papers) | 0.81 | 0.60 |

`chars` separates the two groups (same paper / different papers) more clearly. A pair above the threshold must still pass the margin over the second candidate, the author agreement and the year closeness.

R4 labels on the 2,500-publication pool by threshold (`words` at 0.93 gives 601):

| `chars` threshold | 0.90 | **0.85** | 0.80 | 0.75 |
|---|---|---|---|---|
| R4 labels | 541 | **673** | 739 | 799 |

0.85 was chosen. The 12 new pairs (present at 0.85, absent with `words` at 0.93) were read and all 12 are correct: `blue holonomic…` ↔ `holonomic…`, `deepfake` ↔ `deep fake`, `RingAttention` ↔ `Ring Attention`, `P 5` ↔ `P5`, titles with the year stuck at the end… At 0.75, pairs matching only part of the title begin to appear, for example a title without the system-name prefix (`Grid-Agent: …`) or a renamed version.

**Measured on the 168 manual labels of the Bao range (5 publications):** precision 1.000, recall 1.000 (168/168).
- R1: 31, R2: 55, R3: 79, R4: 3; no wrong label.
- With `words` at 0.93, only 167/168 (`M 2 diffuser` missed).

Important caveat: R3 (allowing a year gap when the authors agree) and the measure and threshold of R4 were all tuned after looking at these same 168 labels. At first R3 required years at most 1 apart and missed 5 journal versions published 2–3 years after the preprint. The numbers above are therefore **optimistic**. The unbiased measurement is the one on the manual labels of the other 4 ranges, which were not used for tuning.

A visual check of 15 random R4 pairs: all 15 correct. Patterns seen: a stray `blue` (a leftover LaTeX colour command), `Embodied SAM` ↔ `EmbodiedSAM`, `^4He` ↔ `4 He`, a title without the `REALM:` prefix.

## Step 4: merging the labels (`merge_labels.py`) and how the labels are used

```bash
# merge every labels_manual_*.csv + labels_auto.csv -> WORK/labels.csv
python src/m3/merge_labels.py --work data/m3_all --suspects data/m3_all/suspect_auto_labels.csv
# while the members are still labelling: the minimums only print warnings
python src/m3/merge_labels.py --work data/m3_all --suspects data/m3_all/suspect_auto_labels.csv --allow-incomplete

# split (Member 1's script); --per-range: each member puts 1 manual + 1 automatic publication into test and into valid
python src/m3/split.py --labels data/m3_all/labels.csv --seed 11 --per-range \
    --stats data/m3_all/split_rankable.csv --out data/m3_all/splits.json

python src/tests/m3/test_merge_labels.py
```

**Output files**

| File | Content |
|---|---|
| `labels.csv` | every decided entry (manual and automatic labels), with the columns of the manual label file plus `range`. The `is_match = false` rows are kept, because the `predictions` table has one row for **every** entry of the publications used for matching (an entry without a reference has `groundtruth_arxiv_id = null`). |
| `label_summary.csv` | per range: publications, entries, manual and automatic labels, the number of automatic publications required |
| `split_rankable.csv` | `arxiv_id, n` for `split.py --stats`. Publications that **must not** go to test/valid get `n = 0`: publications without references, automatic publications without any label, publications with an automatic label known to be wrong (`--suspects`) |

**Checks (an error stops the script, nothing is written)**
- the columns are right; `source` is `manual` or `auto`; `is_match` agrees with `label_ref_arxiv_id`;
- every entry exists in `entries_clean.parquet` and appears once;
- every label is a reference with an arXiv ID of **the same publication**;
- no publication is both manual and automatic; no publication of `manual_pick.csv` is in the automatic labels;
- per range: ≥ 5 manual publications, each with ≥ 20 labels, ≥ 100 manual labels, and automatic publications ≥ 10% of the publications outside the manual pool. With `--allow-incomplete`, these minimums only print warnings.

`data/m3_all/suspect_auto_labels.csv` lists 13 automatic labels known to be wrong or doubtful (see "Final check" in step 3). They stay in training, since 0.02% noise is negligible there, but the publications holding them are not drawn into test/valid, so that MRR is never scored against a wrong answer.

**Official result** (the manual labels of all 5 members, standard mode, every check passed): 2,525 publications, 126,065 entries, **63,612 labels**.

| Range | Manual publications / labels | Automatic publications / labels |
|---|---|---|
| Tram | 5 / 241 | 500 / 12,752 |
| Thu | 5 / 179 | 500 / 12,758 |
| Phat | 5 / 207 | 500 / 12,407 |
| Bao | 5 / 168 | 500 / 12,344 |
| Quan | 5 / 216 | 500 / 12,340 |
| **Total** | **25 / 1,011** | **2,500 / 62,601** |

There are also 62,453 "no match" entries (598 in manual publications, 61,855 in automatic ones): kept for the predictions table, but not samples. 19 publications are kept out of test/valid: publications without any label, or with a doubtful automatic label.

**Checks of the manual labels of 4 members** (on top of the checks of `import`):
- Against the rule hints: only 1 row departs from the hint, and the labeller is right (`PhysRevC.90.057602`: S2 attached the DOI of a 2014 paper to a 2021 paper).
- 11 matched pairs without a hint: read again, all plausible.
- 8 cases of one reference labelled for 2 entries: all are the same work cited twice, with an explanatory note.
- A scan of the labels with an identical title but conflicting authors found **1 wrong label**, now fixed: `NielsenChuang` (the book by Nielsen & Chuang, not on arXiv) was labelled as `1210.0736`, an unrelated paper of the same title by Ya-Zhen Wang (2012).

**Unbiased measurement of the automatic rules** on the manual labels of the 4 ranges never used for tuning: precision **0.999**, recall **0.986** (832 correct, 1 wrong, 12 missed). The only wrong case is a DOI that S2 attached to the wrong paper. The misses are mostly titles of ≤ 2 words (R3 requires ≥ 3 words), renamed papers and transliterated names (Schütz/Schuetz). Details in `auto_vs_manual_4ranges.csv`.

**Split** (`--per-range`, seed 11): each range puts exactly 1 manual + 1 automatic publication into test, and 1 + 1 into valid.

| Set | Manual publications | Automatic publications | Labels | Entries |
|---|---|---|---|---|
| train | 15 | 2,490 | 62,866 | 124,867 |
| valid | 5 | 5 | 400 | 598 |
| test | 5 | 5 | 346 | 600 |

The three sets share no publication; every publication of test/valid has candidates and no doubtful automatic label.

### How the labels are used for features, training and evaluation (Members 3, 4)

As the lecturer answered, **a label is a matched (entry, reference) pair**. An entry with `is_match = false` **is not a sample**.

- **Sample** = each entry with `is_match = true`. Its candidates are **the n references of the same publication** (the rows of `candidates.parquet`):
  - 1 **yes** pair: `(entry, label_ref_arxiv_id)`;
  - n − 1 **no** pairs: the other references of the publication.
- **No** "no" pairs are built from `is_match = false` entries. The automatic rules favour precision and miss some true pairs on purpose; treating those entries as all "no" would teach the model wrong answers.
- **MRR@5:** |Q| = the number of `is_match = true` entries of the test set. The candidates come from the references of the same publication. When a publication has fewer than 5 candidates, the missing `cand_k` cells are null.
- **`predictions` table:** one row for every entry of `labels.csv` (`true` and `false`). The `partition` column comes from `splits.json`; `groundtruth_arxiv_id` = `label_ref_arxiv_id` (empty when `false`).
- **The split** is by publication (`splits.json`); a publication is never in both train and test.
- **Automatic publications in test are scored against labels produced by the rules.** When the model's features include exactly the rules' signals (same ID, same DOI, identical title), MRR on the automatic part is almost bound to be high. MRR should therefore be reported **separately** for the manual and the automatic parts, together with MRR when the link features are masked (see the "shortcut learning" part of `docs/m3_model.md`).
