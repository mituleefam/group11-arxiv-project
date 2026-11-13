# Milestone 2 — GBao full-run review (2026-11-13)

## Scope and provenance

The numbers below are transcribed from the GBao **Colab B11/B12 output supplied by the group**. They do not include independently inspected raw corpus files, so discrepancies must be investigated against the actual `.tex`, `.bib`, `.bbl` and `parsed/` data. The old and new runs each selected 5,000 papers.

| Metric | Old | New |
| --- | ---: | ---: |
| Discovered / with output | 5,000 / 5,000 | 5,000 / 5,000 |
| Parsing success (among discovered) | 100.0% | 100.0% |
| Element occurrences | 2,911,020 | 2,909,835 |
| Distinct elements | 2,190,572 | 2,221,490 |
| Element record reduction | 24.749% | 23.6558% |
| BibTeX entries before dedup | 309,820 | 310,490 |
| BibTeX entries after dedup | 220,838 | 222,682 |
| BibTeX record reduction | 28.7205% | 28.2805% |
| Validation FAIL | 0 | 0 |

New-run validation: **102,586 PASS, 1,386 WARN, 0 FAIL**. These are checks, not unique papers. Reported Parquet file sizes: `elements` 125,793,981 bytes; `hierarchy` 9,730,548 bytes; `bibtex` 22,120,620 bytes; total **157,645,149 bytes**. These are not counterfactual *dedup storage savings*. No reliable new full wall-time or peak-memory numbers were supplied in B11/B12; retrieve `logs/performance_log_m2.csv` to report them.

### Full warning distribution

| Check | Count | Triage |
| --- | ---: | --- |
| gathering_problems | 724 | Review `gathering.json`. Multiple root candidates *alone* are informative if the correct root was selected. Missing input or mis-selected root is significant. |
| nonstandard_leaf_kinds | 540 | Empty `heading`, `item`, `paragraph`, or `list` may be valid or indicate lost content. Inspect source and hierarchy before modifying leaf extraction. |
| citation_key_coverage | 90 | First inspect `0/N` and low-coverage examples. Check source `.bib`/`.bbl`, key renaming, and citation command parsing. |
| appendix_included | 15 | The validator currently detects an appendix macro but expects a heading literally containing `appendix`; `\\appendix` can legitimately precede a section headed `Proof`. The warning is not proof of lost appendix content. |
| acknowledgements_included | 7 | Compare heading text and source, noting environment/macros; do not simply suppress. |
| empty_tex_tree | 6 | High priority: inspect gathered source, cleaned text, and resulting node graph. |
| encoding_replacement_chars | 4 | Inspect U+FFFD in actual source and decoder. |

### Critical sample checks

- `2604.07041`: citation match **0/48**.
- `2604.08146`: citation match **0/20**.
- `2604.08386`: citation match **0/65**.
- `2604.07072`, `2604.07114`, `2604.07242`, `2604.07984`, `2604.08959`: examples of empty TeX tree (6 warnings in total; `2604.07242` has two versions).
- Citation keys beginning `*`, or unusual constructs such as `[following][] segura2003`, may be affected by citation command parsing; inspect raw LaTeX. Never create references based on key strings alone.

### Next steps — do not infer fixes from metrics alone

1. Preserve the **Colab code snapshot**, original `logs/`, `tables/` and validation outputs with run identification; do not overwrite GBao's original result.
2. Run `python src/audit_m2_warnings.py --data-dir DATA_DIR`. Review `logs/warning_triage_m2.csv` and `logs/warning_summary_m2.json`.
3. Inspect the high-priority sample cases against original source. Record each decision as *source issue*, *parser issue*, *validator heuristic*, or *uncertain*. Create regression tests for any confirmed parser defect.
4. If changing `pipeline_m2.py`, `gatherer.py`, `dedup_elements.py`, or reference parser, **reparse with `--force` and repeat validation/statistics/benchmark**. Documentation and an independent audit script alone do not change output datasets.
5. For GitHub, commit source and docs on `m2/tram_fix`; open a PR targeting `m2/main`. Merge only after team review. Never push `data/`, `.venv/`, Parquet corpus or large logs.
6. For the whole group, run all five intervals, export merged tables and aggregate using `src/group_benchmark_m2.py`/`src/group_benchmark.py --m2` with measured export time.

A validation result of zero FAIL is evidence that implemented checks passed, **not** proof of complete citation matching or semantic correctness of every LaTeX document.
