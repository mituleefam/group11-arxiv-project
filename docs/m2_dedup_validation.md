# Milestone 2 — Member 4: Deduplication, Validation & Benchmark

This part covers **the IDs of the LaTeX elements after standardisation**, the validity checks of the output data, and the processing and performance summaries. The code is in `src/`; it does not replace the crawling, hierarchy or reference-parser modules of the other members.

## Files

| File | Content |
| --- | --- |
| `dedup_elements.py` | The Registry that assigns and reuses `element_id` within one publication, across all its versions. |
| `validate_m2.py` | Checks the JSON files of each publication, the tree structure, the citation keys and the schema of the 3 Parquet tables. |
| `statistics_m2.py` | Summarises the parsing coverage, the element/citation counts before and after deduplication, and the error categories. |
| `benchmark_m2.py` | Calls the existing M1 `benchmark.py` to measure the whole M2 pipeline, and adds its own summary log. |
| `tests/m2/test_validate_stats.py` | Tests of the Registry, validation, statistics and integration on synthetic data. |
| `notebooks/m2_benchmark_colab.ipynb` | Runs the tests and the benchmark on Colab CPU-only with the group's M1 corpus. |

## Integration with the pipeline

- `pipeline_m2.py` calls `Registry(arxiv_id).element_id(kind, content, parent_id, occurrence)` while building the nodes and `rows()` to export the `elements` table. **Keep the interface of `m2_contract.py` unchanged.**
- The Registry uses an **exact match after whitespace normalisation**: no lowercasing, no fuzzy matching. A `sentence` / `formula` / `figure` with the same `kind` + `content` shares its ID across versions. Headings and paragraphs also take `occurrence` into account; structural nodes take `parent_id` and `occurrence` into account. The root of each version has its own content.
- The `hierarchy` table stores the edges per version. **Within a version, every node has exactly one parent and no edge is duplicated.** A sentence/formula/figure whose content occurs a second time in the same version gets a new ID; the same content at the corresponding occurrence in another version may share the ID.
- `pipeline_m2.py` calls `registry.start_version(version)` before creating the nodes of each version; the older `element_id`/`rows` interface is kept. The IDs differ from the earlier M2 version, so a `--force` run on the whole corpus is required.
- The references are processed by Member 3 **before** element deduplication, so that the `\cite{...}` keys are unified.

## Checks and outputs

The full Milestone 2 run order, including the step that adds the missing `.bbl` files before parsing, is in `README.md` section 8.2. The commands below are only a trial on 5 publications.

Run them from **the repository root**, with `data/` being an existing Milestone 1 corpus (not part of the source repository):

```bash
python -m unittest discover -s src/tests/m2 -p test_validate_stats.py -v
python src/pipeline_m2.py --data-dir data --limit 5 --workers 2 --force
python src/validate_m2.py --data-dir data --limit 5 --require-parquet
python src/benchmark_m2.py --data-dir data --limit 5 --workers 2 --force
python src/statistics_m2.py --data-dir data --limit 5
```

| Output | Meaning |
| --- | --- |
| `data/corpus/<paper>/parsed/{elements,hierarchy,bibtex}.json` | The pipeline's results; never created by hand. |
| `data/logs/validation_report_m2.csv` | The `PASS` / `WARN` / `FAIL` checks, per publication and version. |
| `data/logs/performance_log_m2.csv` | Wall time, sampled RAM, Parquet size and the run configuration. |
| `data/logs/summary_statistics_m2.csv` | The metrics used in the Jupyter Book. |

**Note for reviewers:** `--limit 5` is only a smoke test; the Parquet files may hold results of earlier runs, so these numbers must not be used as acceptance statistics. For a full JSON–Parquet row-count comparison, run the pipeline, the validator and the statistics on the **whole** corpus, and control any output left over from earlier runs. The official benchmark must run on **Google Colab CPU-only**; numbers from personal computers are not reported.

## Points to review

1. **Registry:** Are IDs reused correctly across versions? Are nodes with the same content but a different `kind` wrongly merged? Are repeated headings and the per-version roots told apart correctly?
2. **Validation:** It checks the root, cycles, orphan nodes, the Arrow/Parquet schema, versions without TeX and unmatched citation keys. A missing citation is recorded as `WARN`, not assumed to be a parser error.
3. **Statistics:** for elements, "before deduplication" is the **number of occurrences** (edges + roots) and "after" the **number of distinct elements**; the reduction in records is **not** a reduction in file size. BibTeX before-deduplication counts exist only when Member 3's `reference_log.csv` is complete.
4. **Benchmark:** `benchmark_m2.py` reuses the M1 `benchmark.py` without changing M1 files. Peak RAM is **the largest sampled value**, not the absolute peak; the measure may count memory pages shared between processes more than once.
5. **Compatibility:** the overall M2 fix changed `gatherer.py`, `pipeline_m2.py`, and the standardisation and BibTeX modules of the other members. The changes have regression tests in `src/tests/m2/test_full_fixes.py`; the M1 `corpus_statistics.py` is unchanged. After merging, run the integration tests, the validation and the benchmark on the whole corpus again.

The CSV/log files and the Parquet tables are run data: **the corpus is never committed to GitHub**. Member 5 only takes the selected statistics tables and logs to build the Jupyter Book figures.
