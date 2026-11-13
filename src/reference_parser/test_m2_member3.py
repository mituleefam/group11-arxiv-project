"""Offline tests of the Milestone 2 Member 3 modules: ref_text, bibitem_parser,
bibtex_converter, dedup_bibtex, citation_updater, and their place in
pipeline_m2 and reference_stats.

    python src/reference_parser/test_m2_member3.py

No network, no pyarrow needed.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # src/
try:
    import m2_contract  # noqa: E402  (Member 1, branch quan-m2-member1)
    BIBTEX_FIELDS = m2_contract.BIBTEX_FIELDS
except ModuleNotFoundError:          # a branch without the Milestone 2 pipeline: the pipeline test is skipped
    m2_contract = None
    BIBTEX_FIELDS = ["arxiv_id", "citation_key", "entry_type", "title", "authors", "year", "doi", "journal", "url"]
from reference_parser.bibitem_parser import cited_keys, parse, parse_bib  # noqa: E402
from reference_parser.bibtex_converter import bib_authors, convert, parse_free_text  # noqa: E402
from reference_parser.citation_updater import update, extend_case_aliases  # noqa: E402
from reference_parser.dedup_bibtex import dedup  # noqa: E402
from reference_parser.ref_text import find_arxiv_id, latex_to_text, normalize_doi, title_key  # noqa: E402

DOC = "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}\n"


def row(key, **fields):
    base = {"citation_key": key, "entry_type": "misc", "title": None, "authors": [], "year": None,
            "doi": None, "journal": None, "url": None, "_source": "structured"}
    base.update(fields)
    return base


class TextTest(unittest.TestCase):
    def test_latex_to_text(self):
        self.assertEqual(latex_to_text(r"G{\"o}del and Erd\H{o}s, \v{S}koda \& Fran\c{c}ois na\"{\i}ve"),
                         "Gödel and Erdős, Škoda & François naïve")
        self.assertEqual(latex_to_text(r"{\em Deep} \textbf{Learning} for {BERT} $x^2$"), "Deep Learning for BERT x^2")
        self.assertEqual(latex_to_text(r"{\L}ukasz Kaiser"), "Łukasz Kaiser")

    def test_identifiers(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1145/3292500.3330701."), "10.1145/3292500.3330701")
        self.assertEqual(normalize_doi("doi:10.1162/NECO.1997.9.8.1735"), "10.1162/neco.1997.9.8.1735")
        for text, arxiv in [("arXiv preprint arXiv:1706.03762", "1706.03762"),
                            ("https://arxiv.org/pdf/1706.03762v5.pdf", "1706.03762"),
                            ("CoRR abs/1810.04805", "1810.04805"), ("arXiv:hep-th/9901001", "hep-th/9901001"),
                            ("10.48550/arXiv.2101.00001", "2101.00001")]:
            self.assertEqual(find_arxiv_id(text), arxiv, text)
        self.assertEqual(title_key("Attention Is {All} You Need!"), "attention is all you need")


class BibFileTest(unittest.TestCase):
    BIB = r"""
% a comment line with @article{commented, title={no}}
@string{nips = "Advances in Neural Information Processing Systems"}
@inproceedings{vaswani2017,
  title     = {Attention is All you Need},
  author    = {Vaswani, Ashish and Shazeer, Noam and others},
  booktitle = nips # " 30",
  year      = 2017,
}
@article{he2016, title = "Deep Residual Learning", author = "Kaiming He and Xiangyu Zhang",
  journal = {CVPR}, year = {2016}, doi = {10.1109/CVPR.2016.90}}
@misc{devlin2018, title={{BERT}: Pre-training}, author={Devlin, Jacob}, eprint={1810.04805},
  archivePrefix={arXiv}, year={2018}}
@inproceedings{part, title={A Part}, crossref={proc}}
@proceedings{proc, title={The Big Proceedings}, year={2020}}
@article{uncited, title={Never cited}}
@comment{ignored}
"""

    def test_bib_fields(self):
        entries = {e["citation_key"]: e for e in parse_bib(self.BIB)}
        self.assertNotIn("commented", entries)
        self.assertEqual(entries["vaswani2017"]["fields"]["booktitle"],
                         "Advances in Neural Information Processing Systems 30")
        self.assertEqual(entries["vaswani2017"]["fields"]["year"], "2017")
        self.assertEqual(entries["he2016"]["fields"]["title"], "Deep Residual Learning")

    def test_only_cited_entries(self):
        text = DOC % r"See \cite{vaswani2017,he2016} and \citep[p.~3]{devlin2018}, \citet*{part}."
        keys = {e["citation_key"] for e in parse(text, {"refs.bib": self.BIB})}
        self.assertEqual(keys, {"vaswani2017", "he2016", "devlin2018", "part"})
        everything = parse(text + r"\nocite{*}", {"refs.bib": self.BIB})
        self.assertIn("uncited", {e["citation_key"] for e in everything})

    def test_rows(self):
        text = DOC % r"\cite{vaswani2017,he2016,devlin2018,part}"
        rows = {r["citation_key"]: r for r in convert(parse(text, {"refs.bib": self.BIB}))}
        v = rows["vaswani2017"]
        self.assertEqual(v["entry_type"], "inproceedings")
        self.assertEqual(v["authors"], ["Vaswani, Ashish", "Shazeer, Noam"])
        self.assertEqual(v["year"], 2017)
        self.assertEqual(v["journal"], "Advances in Neural Information Processing Systems 30")
        self.assertEqual(rows["he2016"]["authors"], ["He, Kaiming", "Zhang, Xiangyu"])
        self.assertEqual(rows["he2016"]["doi"], "10.1109/cvpr.2016.90")
        self.assertEqual(rows["devlin2018"]["title"], "BERT: Pre-training")
        self.assertEqual(rows["devlin2018"]["url"], "https://arxiv.org/abs/1810.04805")
        self.assertEqual(rows["part"]["journal"], "The Big Proceedings")   # crossref
        self.assertEqual(rows["part"]["year"], 2020)
        for r in rows.values():
            self.assertTrue(set(BIBTEX_FIELDS[1:]) <= set(r))

    def test_names(self):
        self.assertEqual(bib_authors(r"Ludwig van Beethoven and {Google Research} and G{\"o}del, Kurt and others"),
                         ["van Beethoven, Ludwig", "Google Research", "Gödel, Kurt"])


class BibitemTest(unittest.TestCase):
    def test_thebibliography(self):
        text = DOC % (r"Text \cite{a,b}." "\n"
                      r"\begin{thebibliography}{99}" "\n"
                      r"\bibitem[{Vaswani et~al.(2017)}]{a} A.~Vaswani, N.~Shazeer, and N.~Parmar." "\n"
                      r"\newblock Attention is all you need." "\n"
                      r"\newblock In {\em Advances in Neural Information Processing Systems}, 2017." "\n"
                      r"\bibitem{b} K.~He, X.~Zhang. Deep residual learning. In CVPR, 2016. \doi{10.1109/CVPR.2016.90}" "\n"
                      r"\end{thebibliography}")
        entries = parse(text, {})
        self.assertEqual([e["citation_key"] for e in entries], ["a", "b"])
        self.assertEqual(entries[0]["label"], "{Vaswani et~al.(2017)}")
        rows = convert(entries)
        self.assertEqual(rows[0]["title"], "Attention is all you need")
        self.assertEqual(rows[0]["authors"], ["Vaswani, A.", "Shazeer, N.", "Parmar, N."])
        self.assertEqual(rows[0]["journal"], "Advances in Neural Information Processing Systems")
        self.assertEqual(rows[0]["entry_type"], "inproceedings")
        self.assertEqual(rows[1]["doi"], "10.1109/cvpr.2016.90")
        self.assertEqual(rows[1]["year"], 2016)

    def test_comments_are_not_references(self):
        text = "\\begin{thebibliography}{9}\n% \\bibitem{x} commented out\n\\bibitem{y} Y. Author. A title. 2001.\n" \
               "\\end{thebibliography}"
        self.assertEqual([e["citation_key"] for e in parse(text, {})], ["y"])

    def test_bibinfo_markup(self):
        raw = (r"\bibinfo{author}{\bibfnamefont{A.}~\bibnamefont{Kitaev}}, \bibinfo{title}{Anyons in an exactly"
               r" solved model}, \bibinfo{journal}{Annals Phys.} \textbf{\bibinfo{volume}{303}}, \bibinfo{year}{2003}.")
        rows = convert(parse("\\begin{thebibliography}{1}\\bibitem{k} " + raw + "\\end{thebibliography}", {}))
        self.assertEqual(rows[0]["title"], "Anyons in an exactly solved model")
        self.assertEqual(rows[0]["authors"], ["Kitaev, A."])
        self.assertEqual(rows[0]["year"], 2003)
        self.assertEqual(rows[0]["journal"], "Annals Phys.")

    def test_free_text_styles(self):
        cases = {
            r"A. Vaswani, N. Shazeer, and N. Parmar, ``Attention is all you need,'' in \emph{Proc. NeurIPS}, 2017.":
                ("Attention is all you need", ["Vaswani, A.", "Shazeer, N.", "Parmar, N."], 2017, "inproceedings"),
            r"Vaswani, A., Shazeer, N., \& Parmar, N. (2017). Attention is all you need. \textit{Advances in Neural"
            r" Information Processing Systems}, 30, 5998--6008.":
                ("Attention is all you need", ["Vaswani, A.", "Shazeer, N.", "Parmar, N."], 2017, "inproceedings"),
            r"Y. LeCun, Y. Bengio, and G. Hinton. Deep learning. \emph{Nature}, 521(7553):436--444, 2015.":
                ("Deep learning", ["LeCun, Y.", "Bengio, Y.", "Hinton, G."], 2015, "article"),
            r"J. Devlin et al. BERT: Pre-training of deep bidirectional transformers. arXiv preprint arXiv:1810.04805, 2018.":
                ("BERT: Pre-training of deep bidirectional transformers", ["Devlin, J."], 2018, "misc"),
            r"C.~M. Bishop. \emph{Pattern Recognition and Machine Learning}. Springer, 2006.":
                ("Pattern Recognition and Machine Learning", ["Bishop, C. M."], 2006, "book"),
            r"Smith J, Doe AB. A study of things. J Chem Phys. 2010;12:1-10.":
                ("A study of things", ["Smith, J.", "Doe, A. B."], 2010, "article"),
            r"S. Hochreiter and J. Schmidhuber, Long short-term memory, Neural Computation 9(8), 1735 (1997).":
                ("Long short-term memory", ["Hochreiter, S.", "Schmidhuber, J."], 1997, "article"),
            r"A.~Einstein, Ann. Phys. \textbf{17}, 891 (1905).":
                (None, ["Einstein, A."], 1905, "article"),
        }
        for raw, (title, authors, year, kind) in cases.items():
            got = parse_free_text(raw)
            self.assertEqual((got["title"], got["authors"], got["year"], got["entry_type"]),
                             (title, authors, year, kind), raw)
        self.assertEqual(parse_free_text(cases and list(cases)[3])["url"], "https://arxiv.org/abs/1810.04805")

    def test_amsrefs(self):
        text = r"\begin{bibdiv}\begin{biblist}\bib{serre}{book}{author={Serre, Jean-Pierre}, title={A course in" \
               r" arithmetic}, date={1973}, publisher={Springer}}\end{biblist}\end{bibdiv}"
        rows = convert(parse(text, {}))
        self.assertEqual((rows[0]["citation_key"], rows[0]["entry_type"], rows[0]["authors"], rows[0]["year"]),
                         ("serre", "book", ["Serre, Jean-Pierre"], 1973))

    def test_bibitem_and_bib_with_the_same_key(self):
        text = DOC % r"\cite{a}\begin{thebibliography}{1}\bibitem{a} A. Author. Some title. 2001.\end{thebibliography}"
        entries = parse(text, {"r.bib": "@article{a, title={Some title}, author={Author, Ann}, doi={10.1000/x}}"})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "bib+bibitem")
        self.assertEqual(convert(entries)[0]["doi"], "10.1000/x")

    def test_cite_variants(self):
        keys, everything = cited_keys(r"\cite{a, b}\citep[see][p.~2]{c}\parencite*{d}\cites[x]{e}[y]{f}\nocite{*}"
                                      r"\citeauthor{a}\Citet{g}")
        self.assertEqual(keys, ["a", "b", "c", "d", "e", "f", "g"])
        self.assertTrue(everything)


class DedupTest(unittest.TestCase):
    def test_task_example(self):
        """The example of the task: one reference through three versions."""
        v1 = [row("vaswani2017", title="Attention is all you need", authors=["Vaswani, Ashish"], year=2017)]
        v2 = [row("vit_transformer", title="Attention Is All You Need", authors=["Vaswani, Ashish"], year=2017,
                  doi="10.5555/3295222.3295349")]
        v3 = [row("vit_transformer", title="Attention is all you need", authors=["Vaswani, Ashish"])]
        rows, rename = dedup({1: v1, 2: v2, 3: v3})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["citation_key"], "vit_transformer")
        self.assertEqual(rename, {"vaswani2017": "vit_transformer"})
        self.assertEqual((r["year"], r["doi"], r["authors"]), (2017, "10.5555/3295222.3295349", ["Vaswani, Ashish"]))
        self.assertEqual(r["title"].lower(), "attention is all you need")

    def test_same_doi_or_arxiv(self):
        rows, rename = dedup({1: [row("a", title="Deep residual learning for image recognition", doi="10.1000/X")],
                              2: [row("b", title="Deep Residual Learning", doi="https://doi.org/10.1000/x")],
                              3: [row("c", url="https://arxiv.org/abs/1512.03385v1"),
                                  row("d", journal="arXiv preprint arXiv:1512.03385")]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rename, {"a": "b", "d": "c"})

    def test_reused_key_for_another_work(self):
        rows, rename = dedup({1: [row("1", title="Attention is all you need", year=2017)],
                              2: [row("1", title="Graph neural networks: a review", year=2020)]})
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["citation_key"] for r in rows}, {"1", "1_2"})
        self.assertEqual(rename, {})

    def test_fuzzy_title_inside_one_version(self):
        rows, rename = dedup({1: [row("k1", title="Generative adversarial nets", authors=["Goodfellow, Ian"], year=2014),
                                  row("k2", title="Generative adversarial networks", authors=["Goodfellow, I."], year=2014),
                                  row("k3", title="Generative models", authors=["Other, A."], year=2014)]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rename), 1)

    def test_different_years_are_different_works(self):
        rows, _ = dedup({1: [row("a", title="Annual report on the state of things", year=2010)],
                         2: [row("b", title="Annual report on the state of things", year=2015)]})
        self.assertEqual(len(rows), 2)

    def test_structured_value_wins(self):
        rows, _ = dedup({1: [row("a", title="Attention is all you need", year=2071, _source="bibitem")],
                         2: [row("a", title="Attention is all you need", year=2017)]})
        self.assertEqual(rows[0]["year"], 2017)


class CitationUpdaterTest(unittest.TestCase):
    def test_unambiguous_case_only_aliases(self):
        text = r"\citep{Zhang2025} and \citet{Zhang2025} and \cite{Unknown}"
        mapping = extend_case_aliases([text], ["zhang2025"], {})
        self.assertEqual(mapping, {"Zhang2025": "zhang2025"})
        self.assertEqual(update(text, mapping),
                         r"\citep{zhang2025} and \citet{zhang2025} and \cite{Unknown}")

    def test_case_aliases_conflicting_or_existing(self):
        text = r"\cite{KEY,Other,Unknown}"
        # A key used for two different works cannot be resolved by case alone.
        mapping = extend_case_aliases([text], ["key", "other"], {"key": "second_work"})
        self.assertNotIn("KEY", mapping)
        self.assertEqual(mapping["Other"], "other")
        self.assertNotIn("Unknown", mapping)

    def test_renames(self):
        rename = {"vaswani2017": "vit", "Old": "new"}
        text = (r"As in \cite{vaswani2017} and \citep[see][p.~3]{ vaswani2017 , other}, \citet*{old}, "
                r"\cites[a]{vaswani2017}[b]{Old}, \cite{vit,vaswani2017}, \nocite{*}.")
        self.assertEqual(update(text, rename),
                         r"As in \cite{vit} and \citep[see][p.~3]{vit,other}, \citet*{new}, "
                         r"\cites[a]{vit}[b]{new}, \cite{vit}, \nocite{*}.")

    def test_nothing_to_do(self):
        text = r"\cite{a} \ref{b}"
        self.assertIs(update(text, {}), text)
        self.assertEqual(update(text, {"b": "c"}), text)   # \ref is not a citation


@unittest.skipIf(m2_contract is None, "m2_contract.py / pipeline_m2.py (Member 1) are not in src/")
class PipelineTest(unittest.TestCase):
    def test_pipeline_normalizes_case_of_bibtex_key(self):
        import pipeline_m2
        tmp = Path(tempfile.mkdtemp())
        try:
            data = tmp / "data"
            paper = data / "corpus" / "2605-00002"
            (paper / "v1").mkdir(parents=True)
            tex = DOC % (r"\section{Introduction}See \citep{Zhang2025} and \citet{Zhang2025}."
                         + "\n\\bibliography{refs}")
            (paper / "v1" / "main.tex").write_text(tex, encoding="utf-8")
            (paper / "v1" / "refs.bib").write_text(
                "@article{zhang2025,title={The Equilibrium Response of Atmospheric Machine Learning Models},"
                "author={Zhang, Bosong and Merlis, Timothy},year={2025}}", encoding="utf-8")
            (paper / "versions.json").write_text(json.dumps({"versions": {"v1": {"status": "success"}}}))
            self.assertEqual(pipeline_m2.main(["--data-dir", str(data), "--workers", "1", "--no-export"]), 0)
            parsed = paper / "parsed"
            aliases = json.loads((parsed / "cite_rename_map.json").read_text())
            self.assertEqual(aliases.get("Zhang2025"), "zhang2025")
            sentences = [r[2] for r in json.loads((parsed / "elements.json").read_text())["rows"]
                         if r[1] == "sentence"]
            self.assertTrue(any(r"\citep{zhang2025}" in t for t in sentences), sentences)
            self.assertFalse(any(r"\citet{Zhang2025}" in t for t in sentences), sentences)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pipeline_and_stats(self):
        import pipeline_m2
        from reference_parser import reference_stats
        comp = m2_contract.load()
        for name in ("bibitem_parser.parse", "bibtex_converter.convert", "dedup_bibtex.dedup",
                     "citation_updater.update"):
            self.assertFalse(any(s.startswith(name) for s in comp.stand_ins), name)
        bib = "@inproceedings{vaswani2017, title={Attention is all you need}, author={Vaswani, Ashish}, year={2017}}"
        bib2 = ("@inproceedings{vit_transformer, title={Attention Is All You Need}, author={Vaswani, Ashish},"
                " year={2017}, doi={10.5555/3295222.3295349}}")
        tmp = Path(tempfile.mkdtemp())
        try:
            data = tmp / "data"
            versions = {"v1": {"main.tex": DOC % "\\section{A}Transformers \\cite{vaswani2017} work.\n"
                                                 "\\bibliography{refs}", "refs.bib": bib},
                        "v2": {"main.tex": DOC % "\\section{A}Transformers \\cite{vit_transformer} work.\n"
                                                 "\\bibliography{refs}", "refs.bib": bib2}}
            paper = data / "corpus" / "2605-00001"
            for v, files in versions.items():
                (paper / v).mkdir(parents=True)
                for rel, text in files.items():
                    (paper / v / rel).write_text(text, encoding="utf-8")
            (paper / "versions.json").write_text(json.dumps({"versions": {v: {"status": "success"} for v in versions}}))
            (data / "work").mkdir(parents=True)
            (data / "work" / "paper_manifest.jsonl").write_text(json.dumps(
                {"arxiv_id": "2605.00001", "status": "success", "versions": list(versions)}) + "\n")
            self.assertEqual(pipeline_m2.main(["--data-dir", str(data), "--workers", "1", "--no-export"]), 0)
            parsed = paper / "parsed"
            bibtex = json.loads((parsed / "bibtex.json").read_text(encoding="utf-8"))["rows"]
            self.assertEqual(len(bibtex), 1)
            self.assertEqual(bibtex[0]["citation_key"], "vit_transformer")
            self.assertEqual(bibtex[0]["doi"], "10.5555/3295222.3295349")
            self.assertEqual(bibtex[0]["arxiv_id"], "2605.00001")
            self.assertEqual(set(bibtex[0]), set(BIBTEX_FIELDS))
            self.assertEqual(json.loads((parsed / "cite_rename_map.json").read_text()),
                             {"vaswani2017": "vit_transformer"})
            elements = json.loads((parsed / "elements.json").read_text(encoding="utf-8"))["rows"]
            sentences = [c for _, kind, c in elements if kind == "sentence"]
            # the renamed \cite makes the sentence of v1 and v2 one element
            self.assertEqual(sentences, ["Transformers \\cite{vit_transformer} work."])

            self.assertEqual(reference_stats.main(["--data-dir", str(data)]), 0)
            with open(data / "logs" / "reference_log.csv", encoding="utf-8") as f:
                logged = list(csv.DictReader(f))
            self.assertEqual((logged[0]["rows_before_dedup"], logged[0]["rows_after_dedup"],
                              logged[0]["renamed_keys"]), ("2", "1", "1"))
            self.assertTrue((data / "logs" / "reference_versions.csv").is_file())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
