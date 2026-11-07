"""Member 3 - Milestone 2: reference extraction and BibTeX standardization.

    ref_text.py          text helpers (LaTeX -> Unicode, DOI / arXiv / URL)
    bbl_converter.py     bbl_parts(text): the references of a .bbl file
    bibitem_parser.py    parse(text, bib_files): \\bibitem, amsrefs, .bib entries of one version
    bibtex_converter.py  convert(entries): rows of the bibtex table
    dedup_bibtex.py      dedup(by_version): one row per reference, key renames
    citation_updater.py  update(text, rename): \\cite keys renamed
    reference_stats.py   CLI: the reference steps alone, with logs
    test_m2_member3.py   offline tests

pipeline_m2.py reaches these modules through m2_contract.load(), which looks
for each module at the top of src/ and then in this package.
See docs/m2_member3_references.md.
"""
