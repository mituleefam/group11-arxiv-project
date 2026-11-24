# M3 – Exporting the Hugging Face dataset

`src/m3/export_hf.py` writes the six tables of the final dataset (`papers`, `references`, `elements`, `hierarchy`,
`bibtex`, `predictions`) as sharded Parquet with the dataset card; `src/m3/validate_m3.py` checks the result against
the statements. Neither script uploads anything: each member publishes with their own Hugging Face token.

```bash
python src/m3/export_hf.py --out HF_DIR \
    --m1-snapshots M1_BACKUP_DIR [M1_BACKUP_DIR ...] \
    --m2-tables M2_MERGED_TABLES \
    --predictions WORK/predictions.parquet \
    [--range START_MONTH START_ID END_MONTH END_ID | --start ID --end ID] \
    --code-url https://github.com/mituleefam/group11-arxiv-project
python src/m3/validate_m3.py --dir HF_DIR
python src/tests/m3/test_export_hf.py
```

## Inputs

| Tables | Option | What to give |
|---|---|---|
| `papers`, `references` | `--m1-snapshots` | the folders of the Milestone 1 snapshot archives (`NNNN_*.tar.gz` of `snapshot.py`), one per range. The archives are replayed in order, as `snapshot.py restore` does, but only `metadata.json` and `references_all.json` are read, so nothing is extracted to disk |
| | `--m1-data-dir` (instead) | restored Milestone 1 data folders (the `--data-dir` of `main.py`) |
| `elements`, `hierarchy`, `bibtex` | `--m2-tables` | the merged tables of `pipeline_m2.py` (a folder holding `elements/`, `hierarchy/`, `bibtex/`), e.g. `/content/drive/MyDrive/benchmark_m2_main/_merged/tables` |
| `predictions` | `--predictions` | `WORK/predictions.parquet` of `src/m3/modeling/predict.py`; its extra `range` column is dropped |

Only the tables whose input is given are written (`--tables` picks a subset), so the tables can be exported in
several runs into the same folder, for example the M1 and M3 tables on a laptop and the M2 tables on Colab: a run
replaces the files of the tables it writes and keeps the others, and the dataset card always lists every table of
the folder. All runs into one folder must use the same range; otherwise the export stops (exit code 2).

## One member's range

`--range` and `--start`/`--end` take the ID range in the notation of `main.py` and keep the papers of that range in
every table (the filter is on `arxiv_id`, the publication; for `references`, the citing paper). IDs are compared
as (year-month, number), so a range across two months works. Without a range, everything given is exported.

```bash
python src/m3/export_hf.py --out hf_Bao --range 2026-04 05535 2026-04 10534 \
    --m1-snapshots benchmark_ms1_main/Bao_2604-05535_2604-10534/snapshots \
    --m2-tables M2_MERGED_TABLES --predictions m3_all/predictions.parquet
```

## Output

```
HF_DIR/
├── README.md                     dataset card: YAML configs (one per table) and the description
└── data/<table>/<table>-NNNNN-of-MMMMM.parquet
HF_DIR.manifest.json              next to the folder (not uploaded): rows, inputs, range, code commit
```

- Column names and types are those of the statements: one-level lists of strings (`authors`, `revised_dates`,
  `ref_authors`), dictionary-encoded `kind`, `entry_type` and `partition`, integer `version` and `year`, non-null
  key columns. A missing input column stops the export instead of writing nulls.
- Files of about `--shard-mb` (200) MB, row groups of 100,000 rows, zstd compression; no loading script.
- `bibtex` is published as frozen in Milestone 2, with its known defect listed in the card;
  `--drop-known-errors` removes the spurious row (`2605.16616`, `aitchison1975statistical`).

## Checks (`validate_m3.py`)

Layout and card configs, exact schema of every file, non-null keys, unique keys, `hierarchy` edges pointing to
`elements`, `predictions` entries being `bibtex` entries, every `groundtruth_arxiv_id` and `cand_*` being a reference
of the same paper, candidates distinct and null only at the end, one prediction row per BibTeX entry of each
predicted paper, file sizes. `--streaming USER/DATASET` also loads every config of the published repository with
`datasets.load_dataset(..., streaming=True)` (needs `pip install datasets`).

Run on the group's data (M1 snapshots of the five ranges and the M3 predictions, 24/11/2026): 25,000 papers,
467,854 references and 126,065 prediction rows, 32 PASS, 0 FAIL. The exported `references` equal
`references_clean.parquet` of the M3 run row for row, so every candidate of `predictions` is in the published
`references`. For Bao's range alone: 5,000 papers, 93,329 references, 25,063 prediction rows (the same as the
`range = Bao` rows of the M3 predictions).

## Publishing

```bash
pip install -U huggingface_hub
huggingface-cli login                       # each member's own token; never commit it
huggingface-cli upload <user>/<dataset> HF_DIR . --repo-type dataset
python src/m3/validate_m3.py --dir HF_DIR --streaming <user>/<dataset>
```

Make the dataset public in its settings on the Hub, then submit its link.
