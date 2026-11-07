"""Offline tests: references kept in a .bbl file, from the archive to the bibtex
table, and the \\label and \\\\ left out of sentences.

    python src/test_m2_bbl.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_bbl
import pipeline_m2
from common import CsvLog, DataLayout
from extractor import NO_TEX, extract_source
from gatherer import gather
from leaf_extractor import extract_leaves
from reference_parser.bbl_converter import bbl_parts
from reference_parser.bibitem_parser import cited_keys, parse
from reference_parser.citation_updater import update
from reference_parser.bibtex_converter import convert
from reference_parser.dedup_bibtex import dedup

DOC = "\\documentclass{article}\n\\begin{document}\n\\section{Intro}\nA claim \\cite{k}.\n%s\n\\end{document}\n"
BBL = ("\\providecommand{\\natexlab}[1]{#1}\n\\begin{thebibliography}{1}\n% \\bibitem{hidden} not read\n"
       "\\bibitem{k} A. Author.\n\\newblock A title from the bbl.\n\\newblock Journal of X, 2020.\n"
       "\\end{thebibliography}\n")
BIBER = r"""\refsection{0}
\datalist[entry]{nty/global//global/global}
  \entry{k}{article}{}
    \name{author}{2}{}{%
      {{hash=a}{%
         family={Smith},
         familyi={S\bibinitperiod},
         given={John\bibnamedelima Paul},
         giveni={J\bibinitperiod\bibinitdelim P\bibinitperiod}}}%
      {{hash=b}{family={Berg},familyi={B\bibinitperiod},given={A},prefix={van\bibnamedelima der}}}%
    }
    \list{publisher}{1}{{ACM}}
    \strng{namehash}{a}
    \field{sortinit}{S}
    \field{journaltitle}{Journal of X}
    \field{title}{A 100\% {Great} title}
    \field{year}{2020}
    \verb{doi}
    \verb 10.1000/xyz%20a
    \endverb
    \verb{url}
    \verb https://a.b/c
    \verb d
    \endverb
  \endentry
\enddatalist
\endrefsection
"""


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, base: Path, files: dict[str, str]) -> Path:
        for name, content in files.items():
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        return base

    def gather(self, files: dict[str, str]):
        return gather(self.write(self.tmp / "v1", files))


class BblParts(unittest.TestCase):
    def test_thebibliography_alone(self):
        bibliography, bibtex = bbl_parts(BBL)
        self.assertTrue(bibliography.startswith("\\begin{thebibliography}"))
        self.assertTrue(bibliography.endswith("\\end{thebibliography}"))
        self.assertNotIn("hidden", bibliography)
        self.assertEqual(bibtex, "")

    def test_cut_off_or_without_environment(self):
        self.assertTrue(bbl_parts("\\begin{thebibliography}{1}\\bibitem{a} T.")[0].endswith("\\end{thebibliography}"))
        self.assertIn("\\begin{thebibliography}", bbl_parts("x\n\\bibitem{a} T.")[0])
        self.assertEqual(bbl_parts("nothing here"), ("", ""))

    def test_biblatex_to_bibtex(self):
        bibliography, bibtex = bbl_parts(BIBER)
        self.assertEqual(bibliography, "")
        rows = convert(parse("\\cite{k}", {"m.bbl": bibtex}))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["citation_key"], row["entry_type"], row["year"]), ("k", "article", 2020))
        self.assertEqual(row["authors"][0], "Smith, John Paul")
        self.assertIn("Berg", row["authors"][1])
        self.assertEqual(row["journal"], "Journal of X")
        self.assertEqual(row["url"], "https://a.b/cd")
        self.assertIn("Great", row["title"])


class SpringerStyle(unittest.TestCase):
    """Springer's bibliography style, common in the .bbl of real papers."""

    def row(self, text: str) -> dict:
        return convert(parse("\\cite{k}\n\\bibitem{k} " + text, {}))[0]

    def test_authors_end_at_the_colon(self):
        row = self.row("Badreddine, S., {d'Avila Garcez}, A., Serafini, L.: Logic tensor networks. "
                       "Artificial Intelligence \\textbf{303}, 103649 (2022)")
        self.assertEqual(row["authors"], ["Badreddine, S.", "d'Avila Garcez, A.", "Serafini, L."])
        self.assertEqual(row["title"], "Logic tensor networks")
        self.assertEqual((row["journal"], row["year"]), ("Artificial Intelligence", 2022))

    def test_a_colon_in_the_title_is_kept(self):
        row = self.row("Devlin, J., Lee, K.: Bert: Pre-training of transformers. In: Doran, C. (eds.) Proc. of NAACL (2019)")
        self.assertEqual(row["title"], "Bert: Pre-training of transformers")
        self.assertEqual(row["journal"], "Proc. of NAACL")

    def test_a_name_without_initials(self):
        self.assertEqual(self.row("Xu, J., Yang, Yang, H.: A technical report here (2023)")["authors"],
                         ["Xu, J.", "Yang", "Yang, H."])

    def test_the_other_styles_stay(self):
        row = self.row("Smith, J. Deep learning: a survey. Journal of X, 2020.")
        self.assertEqual(row["title"], "Deep learning: a survey")
        self.assertEqual(row["authors"], ["Smith, J."])


class OtherStyles(unittest.TestCase):
    """Reference styles of real mathematics, physics and astronomy papers."""

    def row(self, text: str, preamble: str = "") -> dict:
        return convert(parse(preamble + "\\cite{k}\n\\bibitem{k} " + text, {}))[0]

    def assert_row(self, text, title, authors, year, journal, preamble=""):
        row = self.row(text, preamble)
        self.assertEqual((row["title"], row["authors"], row["year"], row["journal"]), (title, authors, year, journal))

    def test_mathematics(self):
        self.assert_row("J. D\u2019Angelo, Real hypersurfaces, orders of contact, and applications, Annals of Mathematics 115 (1982), no. 2, 615--637.",
                        "Real hypersurfaces, orders of contact, and applications", ["D\u2019Angelo, J."], 1982, "Annals of Mathematics")
        self.assert_row("P. Ebenfelt, M. Xiao and H. Xu,  K\u00e4hler-Einstein metrics and obstruction flatness II: unit sphere bundles. J. Funct. Anal. 286 (2024), no. 9, Paper No. 110366, 34 pp.",
                        "K\u00e4hler-Einstein metrics and obstruction flatness II: unit sphere bundles",
                        ["Ebenfelt, P.", "Xiao, M.", "Xu, H."], 2024, "J. Funct. Anal")
        self.assert_row("B. C. Berndt, B. Kim, \nAsymptotic expansions of certain partial theta functions. \nProc. Amer. Math. Soc. 139:11 (2011), 3779-3788.",
                        "Asymptotic expansions of certain partial theta functions", ["Berndt, B. C.", "Kim, B."], 2011, "Proc. Amer. Math. Soc")

    def test_and_after_initials(self):
        self.assert_row("Fernandez, C. and Steel, M. F. J. On Bayesian modelling of fat tails. J. Am. Stat. Assoc. 93 (1998), 359-371.",
                        "On Bayesian modelling of fat tails", ["Fernandez, C.", "Steel, M. F. J."], 1998, "J. Am. Stat. Assoc")

    def test_journal_in_italics(self):
        self.assert_row("J.F. Fernando: On Nash images of Euclidean spaces. {\\em Adv. Math.} {\\bf 331} (2018), 1--10.",
                        "On Nash images of Euclidean spaces", ["Fernando, J.F."], 2018, "Adv. Math")

    def test_family_name_first_without_comma(self):
        self.assert_row("Balinskii A. A. and Novikov S. P., Poisson brackets of hydrodynamic type, {\\it Sov. Math. Dokl.} {\\bf 32} (1985) 228-231.",
                        "Poisson brackets of hydrodynamic type", ["Balinskii, A. A.", "Novikov, S. P."], 1985, "Sov. Math. Dokl")

    def test_title_in_italics(self):
        row = self.row("C. Accettura {\\it et al.}, \\emph{Towards a muon collider}, \\href{https://doi.org/10.1140/epjc/x}{Eur. Phys. J. C 83 (2023) 864}.")
        self.assertEqual((row["title"], row["authors"]), ("Towards a muon collider", ["Accettura, C."]))
        row = self.row("J. Michael Steele {\\it Probability Theory and Combinatorial Optimization}, SIAM, Philadelphia, 1997.")
        self.assertEqual((row["title"], row["authors"], row["year"]), ("Probability Theory and Combinatorial Optimization", ["Steele, J. Michael"], 1997))

    def test_journal_in_italics_is_no_title(self):
        self.assert_row("A. Einstein, {\\it Ann. Phys.} {\\bf 17}, 891 (1905).", None, ["Einstein, A."], 1905, "Ann. Phys")

    def test_physics_stays_without_a_title(self):
        self.assert_row("A.~Einstein, Ann. Phys. \\textbf{17}, 891 (1905).", None, ["Einstein, A."], 1905, "Ann. Phys.")
        self.assert_row("S. Hochreiter and J. Schmidhuber, Long short-term memory, Neural Computation 9(8), 1735 (1997).",
                        "Long short-term memory", ["Hochreiter, S.", "Schmidhuber, J."], 1997, "Neural Computation")

    def test_astronomy(self):
        self.assert_row("White, S.~D.~M., and Rees, M.~J. 1978, MNRAS, 183, 341.", None, ["White, S. D. M.", "Rees, M. J."], 1978, "MNRAS")
        self.assert_row("Springel, V., White, S.~D.~M., et al. 2005, Nature, 435, 629.", None, ["Springel, V.", "White, S. D. M."], 2005, "Nature")
        row = self.row("York, D.~G., Anderson, Jr., J.~E., et al. 2000, AJ, 120, 1579.")
        self.assertEqual((row["title"], row["journal"], row["entry_type"], len(row["authors"])), (None, "AJ", "article", 2))
        self.assert_row("Smith, J.~A. 2010, \\apj, 700, 1.", None, ["Smith, J. A."], 2010, "ApJ")
        self.assertEqual(self.row("Smith, J. 2010, A long title of the paper, ApJ, 700, 1.")["title"], "A long title of the paper")

    def test_journal_with_commas_is_no_title(self):
        self.assert_row("Chen, Z., and Yu, Y. 2025, Science China Physics, Mechanics, and Astronomy, 68, 109513", None,
                        ["Chen, Z.", "Yu, Y."], 2025, "Science China Physics, Mechanics, and Astronomy")

    def test_numbered_parts_are_different_references(self):
        rows = convert(parse("\\cite{a,b,c,d}\n"
                             "\\bibitem{a} J. Kohn, Harmonic integrals on strongly pseudo-convex manifolds, Ann. of Math. 78 (1963), 112.\n"
                             "\\bibitem{b} J. Kohn, Harmonic integrals on strongly pseudo-convex manifolds II, Ann. of Math. 79 (1964), 450.\n"
                             "\\bibitem{c} A. Touvron, Llama 2: open foundation and fine-tuned chat models, arXiv preprint, J. X 1 (2023).\n"
                             "\\bibitem{d} A. Touvron, Llama 3: open foundation and fine-tuned chat models, arXiv preprint, J. X 2 (2023).\n", {}))
        merged, rename = dedup({1: rows})
        self.assertEqual((len(merged), rename), (4, {}))

    def test_macros_of_the_paper(self):
        preamble = ("\\def\\article{\\@ifnextchar[{\\earticle}{\\oarticle}}\n"
                    "\\def\\oarticle#1#2#3#4#5#6{#1, {\\it `#6'}, #2 {\\bf #3} (#5) #4}\n"
                    "\\def\\earticle[#1]#2#3#4#5#6#7{#2, {\\it `#7'}, #3 {\\bf #4} (#6) #5 [arXiv:#1]}\n"
                    "\\newcommand{\\jhep}{JHEP}\n\\newcommand{\\both}[2][x]{#1 and #2}\n"
                    "\\newcommand{\\doi}[1]{DOI GONE}\n")
        row = self.row("\\article{S. Ferrara, D. Lust}{Phys.Lett.B}{225}{363}{1989}{Modular invariance in field theories}.", preamble)
        self.assertEqual((row["title"], row["year"], row["authors"]), ("Modular invariance in field theories", 1989, ["Ferrara, S.", "Lust, D."]))
        row = self.row("\\article[hep-th/9202046]{L. Ibanez}{Nucl.Phys.B}{382}{305}{1992}{Duality anomaly cancellation here}.", preamble)
        self.assertEqual((row["title"], row["year"]), ("Duality anomaly cancellation here", 1992))
        self.assertIn("A title", self.row("A. Author, A title, \\jhep 01 (2020) 1. \\doi{10.1000/x}", preamble)["title"])
        self.assertEqual(self.row("A. Author, A title, \\jhep 01 (2020) 1. \\doi{10.1000/x}", preamble)["doi"], "10.1000/x")

    def test_tex_programming_is_not_expanded(self):
        preamble = ("\\providecommand{\\foreignlanguage}[2]{\\expandafter\\ifx\\csname l@#1\\endcsname\\relax "
                    "\\typeout{** WARNING} #2\\fi}\n\\def\\odd{\\ifx a b\\fi}\n")
        row = self.row("A. Author, \\foreignlanguage{en}{A title in a language}, J. X 1 (2000).", preamble)
        self.assertEqual(row["title"], "A title in a language")

    def test_a_macro_calling_itself_stops(self):
        self.assertTrue(self.row("\\loop A. Author, A title here, J. X 1 (2000).", "\\def\\loop{\\loop x}\n"))


class Gathering(Folder):
    def test_bibliography_without_bib_reads_the_root_bbl(self):
        g = self.gather({"m.tex": DOC % "\\bibliography{refs}", "m.bbl": BBL})
        self.assertEqual((g.bbl_files, g.bib_files, g.has_thebibliography, g.needs_bbl), (["m.bbl"], [], True, False))
        self.assertFalse(any(".bbl" in p for p in g.problems))
        self.assertNotIn("providecommand", g.text)

    def test_bbl_named_after_the_bibliography_or_alone(self):
        self.assertEqual(self.gather({"m.tex": DOC % "\\bibliography{refs}", "refs.bbl": BBL}).bbl_files, ["refs.bbl"])
        shutil.rmtree(self.tmp / "v1")
        self.assertEqual(self.gather({"m.tex": DOC % "\\bibliography{refs}", "out/other.bbl": BBL}).bbl_files,
                         ["out/other.bbl"])

    def test_two_bbl_files_are_not_guessed(self):
        g = self.gather({"m.tex": DOC % "\\bibliography{refs}", "a.bbl": BBL, "b.bbl": BBL})
        self.assertEqual(g.bbl_files, [])
        self.assertTrue(g.needs_bbl)

    def test_input_bbl_is_read_and_is_not_a_root_candidate(self):
        g = self.gather({"m.tex": DOC % "\\input{m.bbl}", "m.bbl": BBL})
        self.assertEqual((g.status, g.root, g.bbl_input, g.bbl_files), ("ok", "m.tex", True, ["m.bbl"]))
        self.assertEqual(g.files, ["m.tex"])

    def test_input_bib_is_a_bibliography(self):
        g = self.gather({"m.tex": DOC % "\\input{r.bib}", "r.bib": "@misc{k, title={T}}"})
        self.assertEqual((g.status, g.bib_files), ("ok", ["r.bib"]))
        self.assertNotIn("@misc", g.text)

    def test_the_bib_wins_over_a_stale_bbl(self):
        g = self.gather({"m.tex": DOC % "\\bibliography{r}", "r.bib": "@misc{k,}", "m.bbl": BBL})
        self.assertEqual((g.bib_files, g.bbl_files, g.has_thebibliography), (["r.bib"], [], False))

    def test_biblatex_bbl_becomes_embedded_bib(self):
        g = self.gather({"m.tex": DOC % "\\addbibresource{r.bib}\\printbibliography", "m.bbl": BIBER})
        self.assertEqual(g.bbl_files, ["m.bbl"])
        self.assertIn("@article{k", g.embedded_bib["m.bbl"])
        self.assertNotIn("\\entry", g.text)

    def test_needs_bbl(self):
        self.assertTrue(self.gather({"m.tex": DOC % "\\bibliography{r}"}).needs_bbl)
        shutil.rmtree(self.tmp / "v1")
        self.assertTrue(self.gather({"m.tex": DOC % ""}).needs_bbl)  # cites, no bibliography declared
        shutil.rmtree(self.tmp / "v1")
        self.assertFalse(self.gather({"m.tex": "\\documentclass{a}\\begin{document}No citation.\\end{document}"}).needs_bbl)

    def test_undeclared_bibliography_falls_back_to_the_bbl(self):
        g = self.gather({"m.tex": DOC % "", "m.bbl": BBL})
        self.assertEqual((g.bbl_files, g.has_thebibliography), (["m.bbl"], True))


class Extraction(Folder):
    def archive(self, files: dict[str, str]) -> Path:
        path = self.tmp / "source.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for name, content in files.items():
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def test_bbl_is_kept(self):
        dest = self.tmp / "out"
        dest.mkdir()
        result = extract_source(self.archive({"m.tex": "x", "m.bbl": "y", "fig.png": "z"}), dest)
        self.assertEqual(result.kept_files, ["m.bbl", "m.tex"])

    def test_a_bbl_alone_is_no_source(self):
        dest = self.tmp / "out"
        dest.mkdir()
        result = extract_source(self.archive({"m.bbl": "y", "paper.docx": "z"}), dest)
        self.assertEqual((result.status, result.kept_files, result.kept_bytes), (NO_TEX, [], 0))
        self.assertEqual(list(dest.iterdir()), [])


class PaperFolder(Folder):
    def paper(self, versions: dict[str, dict[str, str]]) -> DataLayout:
        layout = DataLayout(self.tmp / "data")
        paper = layout.corpus / "2604-00001"
        records = {}
        for version, files in versions.items():
            self.write(paper / version, files)
            records[version] = {"status": "success", "kept_files": sorted(files),
                                "kept_bytes": sum(len(c.encode()) for c in files.values())}
        layout.logs.mkdir(parents=True)
        (paper / "versions.json").write_text(json.dumps({"arxiv_id": "2604.00001", "versions": records}))
        return layout

    def parse(self, layout: DataLayout) -> dict:
        paper = layout.corpus / "2604-00001"
        record = pipeline_m2.parse_paper({"arxiv_id": "2604.00001", "paper_dir": str(paper),
                                          "versions": [], "code": "t"})
        self.assertEqual(record["status"], "done", record.get("error"))
        return {name: json.loads((paper / "parsed" / f"{name}.json").read_text(encoding="utf-8"))
                for name in ("elements", "bibtex", "gathering")}


class Pipeline(PaperFolder):
    def test_references_of_a_bbl_reach_the_table_and_stay_out_of_the_tree(self):
        parsed = self.parse(self.paper({"v1": {"m.tex": DOC % "\\bibliography{refs}", "m.bbl": BBL}}))
        row = parsed["bibtex"]["rows"][0]
        self.assertEqual((row["citation_key"], row["title"], row["year"]), ("k", "A title from the bbl", 2020))
        contents = " ".join(e[2] for e in parsed["elements"]["rows"])
        self.assertNotIn("title from the bbl", contents)
        self.assertEqual(parsed["gathering"]["v1"]["bbl_files"], ["m.bbl"])

    def test_label_and_protect_leave_titles_and_items(self):
        v1 = "\\documentclass{a}\\begin{document}\\section{Proof of \\protect\\ref{t}\\protect\\label{s:a}}\nOne here.\n" \
             "\\begin{itemize}\\item[\\label{i:a}First] Item here.\\end{itemize}\n\\paragraph{Named\\label{p:a}} Two here.\n" \
             "\\section*{References\\label{r}}\nDropped here.\n\\end{document}"
        v2 = v1.replace("s:a", "s:b").replace("i:a", "i:b").replace("p:a", "p:b")
        rows = self.parse(self.paper({"v1": {"m.tex": v1}, "v2": {"m.tex": v2}}))["elements"]["rows"]
        self.assertEqual([e[2] for e in rows if e[1] == "heading"], ["Proof of \\ref{t}"])
        self.assertEqual([e[2] for e in rows if e[1] == "paragraph"], ["Named"])
        self.assertEqual([e[2] for e in rows if e[1] == "item"], ["First"])
        self.assertNotIn("Dropped here.", [e[2] for e in rows])

    def test_cite_renamed_across_a_bbl_and_a_bib(self):
        v1 = {"m.tex": DOC % "\\bibliography{refs}", "m.bbl": BBL}
        v2 = {"m.tex": (DOC % "\\bibliography{r}").replace("\\cite{k}", "\\cite{new}"),
              "r.bib": "@article{new, title={A title from the bbl}, author={Author, A.}, year={2020}, doi={10.1000/x}}"}
        parsed = self.parse(self.paper({"v1": v1, "v2": v2}))
        self.assertEqual(len(parsed["bibtex"]["rows"]), 1)
        self.assertEqual(parsed["bibtex"]["rows"][0]["doi"], "10.1000/x")
        sentences = [e for e in parsed["elements"]["rows"] if e[1] == "sentence"]
        self.assertEqual(len(sentences), 1)  # the same sentence once the key is unified


class Refetch(PaperFolder):
    def fake_fetch(self, sources: dict[str, dict[str, str]], calls: list[str]):
        def fetch(session, versioned_id, target, *, limiter=None):
            calls.append(versioned_id)
            if versioned_id not in sources:
                return SimpleNamespace(ok=False, http_status=404, error="HTTP 404")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(target, "w:gz") as tar:
                for name, content in sources[versioned_id].items():
                    data = content.encode("utf-8")
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            return SimpleNamespace(ok=True, http_status=200, error="")
        original = fetch_bbl.fetch_source
        fetch_bbl.fetch_source = fetch
        self.addCleanup(setattr, fetch_bbl, "fetch_source", original)

    def test_adds_only_the_bbl_and_records_it(self):
        tex = DOC % "\\bibliography{refs}"
        layout = self.paper({"v1": {"m.tex": tex}})
        paper = layout.corpus / "2604-00001"
        found = fetch_bbl.scan_paper(str(paper))
        self.assertEqual(found["versions"], ["v1"])
        calls = []
        self.fake_fetch({"2604.00001v1": {"m.tex": "CHANGED ON THE SERVER", "m.bbl": BBL, "fig.png": "z"}}, calls)
        counts = fetch_bbl.run([found], layout, session=object(), limiter=None, workers=1)
        self.assertEqual(counts, {"added": 1})
        self.assertEqual((paper / "v1" / "m.tex").read_text(encoding="utf-8"), tex)  # never replaced
        self.assertFalse((paper / "v1" / "fig.png").exists())
        record = json.loads((paper / "versions.json").read_text())["versions"]["v1"]
        self.assertEqual(record["kept_files"], ["m.bbl", "m.tex"])
        self.assertEqual(record["kept_bytes"], sum(p.stat().st_size for p in (paper / "v1").iterdir()))
        self.assertNotIn("attempts_total", record)
        self.assertEqual(fetch_bbl.scan_paper(str(paper))["versions"], [])  # nothing left to ask
        self.assertEqual(self.parse(layout)["bibtex"]["rows"][0]["citation_key"], "k")
        self.assertEqual(list((layout.work / "downloads").iterdir()), [])

    def test_a_stopped_run_is_completed(self):
        layout = self.paper({"v1": {"m.tex": DOC % "\\bibliography{refs}"}})
        paper = layout.corpus / "2604-00001"
        (paper / "v1" / "m.bbl").write_bytes(BBL.encode("utf-8"))  # copied, then the run stopped
        found = fetch_bbl.scan_paper(str(paper))
        self.assertEqual((found["versions"], found["unrecorded"]), ([], 1))
        self.assertEqual(fetch_bbl.record_bbl(paper, "2604.00001", "v1"), (1, len(BBL.encode("utf-8"))))
        record = json.loads((paper / "versions.json").read_text())["versions"]["v1"]
        self.assertEqual(record["kept_files"], ["m.bbl", "m.tex"])
        self.assertEqual(record["kept_bytes"], sum(p.stat().st_size for p in (paper / "v1").iterdir()))
        self.assertEqual(fetch_bbl.scan_paper(str(paper))["unrecorded"], 0)

    def test_no_bbl_and_gone_are_not_asked_again(self):
        layout = self.paper({"v1": {"m.tex": DOC % "\\bibliography{r}"}, "v2": {"m.tex": DOC % "\\bibliography{r}"}})
        paper = layout.corpus / "2604-00001"
        self.fake_fetch({"2604.00001v1": {"m.tex": "x"}}, [])
        counts = fetch_bbl.run([fetch_bbl.scan_paper(str(paper))], layout, session=object(), limiter=None, workers=1)
        self.assertEqual(counts, {"no_bbl": 1, "not_found": 1})
        self.assertEqual(fetch_bbl.settled_versions(layout.logs / "bbl_log.csv"),
                         {("2604.00001", "v1"), ("2604.00001", "v2")})

    def test_failed_is_asked_again(self):
        log = CsvLog(self.tmp / "bbl_log.csv", fetch_bbl.LOG_FIELDS)
        log.write(arxiv_id="a", version="v1", status="no_bbl")
        log.write(arxiv_id="a", version="v1", status="failed")
        self.assertEqual(fetch_bbl.settled_versions(self.tmp / "bbl_log.csv"), set())


class Sentences(unittest.TestCase):
    def test_label_and_line_break_leave_the_sentence(self):
        self.assertEqual(extract_leaves("\\label{sec:a} Our model works. It is\\label{x} fast.\\\\ Next one.\\\\[2mm] Last."),
                         [("sentence", "Our model works."), ("sentence", "It is fast."),
                          ("sentence", "Next one."), ("sentence", "Last.")])

    def test_formula_figure_and_math_keep_theirs(self):
        leaves = extract_leaves("See $a \\\\ b$ here.\n\\begin{equation}\\label{e} x \\\\ y \\end{equation}\n"
                                "\\begin{figure}\\caption{C}\\label{f}\\end{figure}")
        self.assertEqual(leaves[0], ("sentence", "See $a \\\\ b$ here."))
        self.assertIn("\\label{e}", leaves[1][1])
        self.assertIn("\\label{f}", leaves[2][1])

    def test_font_macros_of_a_bbl_are_no_citations(self):
        text = ("\\cite{k}\n\\begin{thebibliography}{1}\n"
                "\\bibitem[{\\citenamefont{Bell}(2008)}]{k} \\citenamefont{A. Bell}, \\citenumfont{12}, J. X 1 (2008).\n"
                "\\bibitem[\\protect\\citeauthoryear{Bell}{2008}]{m} A. Bell, J. Y 2 (2008).\n"
                "\\end{thebibliography}\n")
        self.assertEqual(cited_keys(text)[0], ["k"])
        self.assertEqual(update("\\citenamefont{k} and \\cite{k}", {"k": "new"}), "\\citenamefont{k} and \\cite{new}")

    def test_only_a_label_is_no_sentence(self):
        self.assertEqual(extract_leaves("\\label{a}"), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
