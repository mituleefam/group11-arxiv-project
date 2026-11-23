"""Regression coverage for all six mandatory M2 fixes and selected quality fixes.

Synthetic documents only: these tests prove the specified edge cases, not the
full 5x5000-paper corpus or final Google Colab CPU performance.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cleaner import clean
from dedup_elements import Registry
from gatherer import gather
from group_benchmark_m2 import aggregate
from hierarchy import build
from leaf_extractor import extract_leaves
from normalizer import normalize
from reference_parser.bbl_converter import bbl_parts
from reference_parser.bibitem_parser import parse
from reference_parser.bibtex_converter import convert, from_fields, to_year
from reference_parser.dedup_bibtex import dedup
from sentence_splitter import split_sentences
from validate_m2 import Reporter, _verify_graph

DOC = r"\documentclass{article}\begin{document}\section{Intro}\cite{k}.\bibliography{main}\end{document}"


class FullM2FixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "v1"
        self.folder.mkdir()

    def write(self, name, content):
        f = self.folder / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")

    def test_same_sentence_in_different_parents_is_two_nodes_in_one_version(self):
        reg = Registry("2604.05537")
        reg.start_version(1)
        root = reg.element_id("document", "Version 1 root", None, 0)
        tree = build(r"\documentclass{article}\begin{document}\section{A} OBDD."
                     r"\section{B} OBDD.\end{document}", root, reg, extract_leaves)
        ids = [r["element_id"] for r in reg.rows() if r["content"] == "OBDD."]
        self.assertEqual(len(ids), 2)
        reporter = Reporter()
        _verify_graph("2604.05537", 1, {r["element_id"]:r for r in reg.rows()}, root, tree.edges, reporter)
        self.assertFalse(any(r["status"] == "FAIL" for r in reporter.rows), reporter.rows)
        reg.start_version(2)
        root2 = reg.element_id("document", "Version 2 root", None, 0)
        tree2 = build(r"\documentclass{article}\begin{document}\section{New} OBDD."
                      r"\section{Other} OBDD.\end{document}", root2, reg, extract_leaves)
        self.assertEqual(len([r for r in reg.rows() if r["content"] == "OBDD."]), 2)
        self.assertEqual({c for c, _ in tree.edges if c in ids},
                         {c for c, _ in tree2.edges if c in ids})

    def test_validator_rejects_multiparent_and_exact_duplicate(self):
        reg = Registry("2604.05537")
        root = reg.element_id("document", "Version 1 root", None, 0)
        a = reg.element_id("heading", "A", None, 0)
        b = reg.element_id("heading", "B", None, 0)
        s = reg.element_id("sentence", "A sentence.", a, 0)
        elements = {r["element_id"]:r for r in reg.rows()}
        reporter = Reporter()
        _verify_graph("2604.05537", 1, elements, root,
                      [(a,root),(b,root),(s,a),(s,b),(s,a)], reporter)
        status = {r["check_name"]:r["status"] for r in reporter.rows}
        self.assertEqual(status["unique_parent_per_version"], "FAIL")
        self.assertEqual(status["duplicate_hierarchy_edges"], "FAIL")

    def test_bibliography_command_with_only_bbl(self):
        self.write("main.tex", DOC)
        self.write("Main.bbl", r"\begin{thebibliography}{9}"
                   r"\bibitem{k} Alice Smith. A source reference. Journal Y, 2021."
                   r"\end{thebibliography}")
        gathered = gather(self.folder)
        self.assertEqual(gathered.bib_files, [])
        self.assertEqual(gathered.bbl_files, ["Main.bbl"])
        self.assertIn("\\bibitem{k}", gathered.text)
        refs = convert(parse(gathered.text, {}))
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["citation_key"], "k")

    def test_explicit_bbl_command(self):
        self.write("main.tex", DOC.replace(r"\bibliography{main}", r"\bibliography{main.bbl}"))
        self.write("main.bbl", r"\bibitem{k} Jane Smith. Some source work. 2020.")
        gathered = gather(self.folder)
        self.assertEqual(gathered.bbl_files, ["main.bbl"])
        self.assertFalse(gathered.bib_files)

    def test_amsrefs_bbl(self):
        self.write("main.tex", DOC)
        self.write("main.bbl", r"\bib{arxivpaper}{article}{author={Jane Smith}, "
                   r"title={An Observational Study of Galaxies}, year={2020}, journal={Astrophysics}}")
        gathered = gather(self.folder)
        self.assertIn(r"\bib{arxivpaper}", gathered.text)
        refs = convert(parse(gathered.text.replace(r"\cite{k}", r"\cite{arxivpaper}"), {}))
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["year"], 2020)
        self.assertIn("Galaxies", refs[0]["title"])

    def test_harvard_bbl(self):
        self.write("main.tex", DOC)
        self.write("main.bbl", (r"\begin{thebibliography}{9}"
                   r"\harvarditem[Smith et al.]{J. Smith}{2023}{k} "
                   r"An Account of Numerical Methods. Journal X."
                   r"\end{thebibliography}"))
        gathered = gather(self.folder)
        self.assertIn(r"\bibitem{k}", gathered.text)
        refs = convert(parse(gathered.text, {}))
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["year"], 2023)

    def test_multiple_roots_prioritizes_main_over_diff_and_supp(self):
        self.write("supp_v1.tex", r"\documentclass{article}\begin{document}Suppl." + "long "*2000 + r"\end{document}")
        self.write("manuscript_latexdiff.tex", r"\documentclass{article}\begin{document}Draft.\end{document}")
        self.write("main_v1.tex", r"\documentclass{article}\begin{document}Real article.\end{document}")
        self.assertEqual(gather(self.folder).root, "main_v1.tex")

    def test_generic_atlas_and_gpt_titles_not_deduped(self):
        def row(key, title, year=None, authors=None, doi=None):
            return {"citation_key":key, "title":title, "year":year, "authors":authors or [],
                    "doi":doi,"entry_type":"article","url":None,"journal":None}
        rows, ren = dedup({1: [row("zz", "(ATLAS Collaboration)", 2020)],
                           2: [row("zz", "(ATLAS Collaboration)", 2020)]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(ren, {})
        rows, ren = dedup({1: [row("k", "GPT-5.4 technical report")],
                           2: [row("k", "GPT-4 Turbo technical report")],
                           3: [row("k", "GPT-3.5 technical report")]})
        self.assertEqual(len(rows), 3)
        self.assertEqual(ren, {})
        rows, _ = dedup({1: [row("a", "This is a Reliable Study", 2020, ["Smith, J."], "10.1000/a")],
                         2: [row("b", "This is a Reliable Study", 2020, ["Smith, J."], "10.1000/a")]})
        self.assertEqual(len(rows), 1)

    def test_year_and_journal_macro(self):
        self.assertIsNone(to_year("1805.02656"))
        self.assertEqual(to_year("Newton 1687."), 1687)
        self.assertEqual(to_year("published 2017."), 2017)
        row = from_fields({"title":"Sample", "year":"2020", "journal":r"\apj"}, "article")
        self.assertEqual(row["journal"], "ApJ")
        self.assertEqual(row["year"], 2020)

    def test_texorpdfstring_and_sentence_boundary(self):
        self.assertEqual(clean(r"\section{\texorpdfstring{$E=mc^2$}{Energy}}"),
                         r"\section{$E=mc^2$}")
        self.assertEqual(split_sentences("As noted by Dabrowski et al. The result follows."),
                         ["As noted by Dabrowski et al.", "The result follows."])
        self.assertEqual(split_sentences("Smith et al. introduced the algorithm."),
                         ["Smith et al. introduced the algorithm."])


class GroupBenchM2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def member(self, name, papers, before, after, bib_before, bib_after, wall, ram):
        root = self.root / name
        logs = root / "logs"
        logs.mkdir(parents=True)
        vals = {"discovered_papers_selected":papers, "papers_with_output":papers,
                "papers_not_parsed":0, "papers_missing_or_invalid_output":0,
                "versions_with_record":papers*2, "versions_with_tex":papers*2,
                "elements_occurrences_before_dedup":before,
                "elements_unique_after_dedup":after, "hierarchy_edges":before-papers*2,
                "bibtex_entries_before_dedup":bib_before,
                "bibtex_entries_after_dedup":bib_after, "validation_fail_checks":0}
        with (logs/"summary_statistics_m2.csv").open("w", newline="") as f:
            w=csv.DictWriter(f, fieldnames=["metric","value"]);w.writeheader()
            w.writerows({"metric":k,"value":v} for k,v in vals.items())
        with (logs/"performance_log_m2.csv").open("w", newline="") as f:
            w=csv.DictWriter(f,fieldnames=["run_id","paper_limit","force_reparse","status",
                                              "exit_code","ended_utc","wall_seconds","ram_sampled_peak_bytes"])
            w.writeheader();w.writerow({"run_id":name,"paper_limit":"all","force_reparse":1,
                "status":"finished","exit_code":0,"ended_utc":"2026-11-03T00:00:00Z",
                "wall_seconds":wall,"ram_sampled_peak_bytes":ram})
        import os
        from time import time
        # Statistics must be written after the benchmark (production run order).
        os.utime(logs / "summary_statistics_m2.csv", (time()+2, time()+2))
        return name, root

    def test_group_aggregates_counts_and_wall_then_uses_final_disk(self):
        members = [self.member("A", 10, 100, 60, 40, 30, 20, 1000),
                   self.member("B", 20, 200, 150, 80, 60, 40, 2000)]
        table = self.root/"merged"
        for name, n in (("elements", 27),("hierarchy",31),("bibtex",17)):
            folder = table / name; folder.mkdir(parents=True)
            (folder/"part-00000.parquet").write_bytes(b"x"*n)
        export = self.root/"export_summary.json"
        export.write_text(json.dumps({"wall_seconds":10, "ram_sampled_peak_bytes":3000,
                                      "command":["python","src/pipeline_m2.py","--export-only"],
                                      "return_code":0,"interrupted":False}))
        rows, group, warn = aggregate(members, table, export)
        self.assertEqual(warn, [])
        self.assertEqual(group["completeness"], "complete")
        self.assertEqual(group["wall_seconds"], 70)
        self.assertEqual(group["ram_peak_bytes"], 3000)
        self.assertEqual(group["parquet_final_bytes"], 75)
        self.assertAlmostEqual(group["element_reduction_percent"], 30.0)
        self.assertAlmostEqual(group["bibtex_reduction_percent"], 25.0)
        self.assertEqual(group["papers"], 30)

    def test_group_rejects_missing_member_benchmark_and_double_folder(self):
        member = self.member("A", 10, 100, 50, 20, 10, 20, 500)
        with self.assertRaises(ValueError):
            aggregate([member, ("B", member[1])], None, None)
        (member[1]/"logs"/"performance_log_m2.csv").unlink()
        rows, group, warnings = aggregate([member], None, None)
        self.assertEqual(group["completeness"], "incomplete")
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
