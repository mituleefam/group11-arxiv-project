"""Offline tests of the Milestone 2 Member 1 modules: latex_scan, gatherer,
hierarchy and pipeline_m2 (with the m2_contract stand-ins).

    python src/test_m2_member1.py

No network. The pipeline test writes Parquet tables when pyarrow is
installed, and checks the parsed/ files only otherwise.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gatherer import NO_SOURCE, NO_TEX, OK, gather  # noqa: E402
from hierarchy import build, split_paragraphs  # noqa: E402
from latex_scan import find_env_end, read_group, read_optional, strip_comments  # noqa: E402
from m2_contract import BasicRegistry, extract_leaves_basic  # noqa: E402

DOC = "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}\n"


def stripped(text):
    return strip_comments(text)[0]


class LatexScanTest(unittest.TestCase):
    def test_comments_as_tex_reads_them(self):
        cases = {
            "a%c\nb": "ab",                      # the end of line goes with the comment
            "word %c\nnext": "word next",
            "50\\% off": "50\\% off",            # escaped percent
            "\\\\%c\nx": "\\\\x",                # line break, then a comment
            "\\foo%\nbar": "\\foo bar",          # \foo then bar, not \foobar
            "line1\n% comment\nline2": "line1\nline2",   # no blank line made
            "p1.%\n\np2.": "p1.\n\np2.",         # the blank line after it stays
            "a%c\r\nb": "ab",
            "\\fill % c\nx": "\\fill x",          # \fill is not \fi
            "\\verb|%x| y % c\nz": "\\verb|%x| y z",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(stripped(text), expected)

    def test_iffalse(self):
        self.assertEqual(stripped("A \\iffalse B \\fi C"), "A  C")
        self.assertEqual(stripped("A \\iffalse B \\else C \\fi D"), "A  C  D")
        self.assertEqual(stripped("\\iffalse x \\ifx\\a\\b y \\fi z \\fi w"), " w")
        self.assertEqual(stripped("\\iffalse a % \\fi\n b \\fi c"), " c")
        self.assertEqual(stripped("\\iffalse \\ifthenelse{x}{y}{z} \\fi ok"), " ok")
        self.assertEqual(stripped("\\iffalse a \\else b \\ifx c \\fi d \\fi e"), " b \\ifx c \\fi d  e")
        text, problems = strip_comments("x \\iffalse never closed")
        self.assertEqual(text, "x \\iffalse never closed")
        self.assertTrue(problems)

    def test_comment_and_verbatim_environments(self):
        self.assertEqual(stripped("a\n\\begin{comment}\nhidden \\input{x}\n\\end{comment}\nb"), "a\n\nb")
        verb = "\\begin{verbatim}\n% not a comment \\input{x}\n\\end{verbatim}"
        self.assertEqual(stripped(verb), verb)

    def test_groups_and_environments(self):
        self.assertEqual(read_group("  {a{b}c} rest", 0), ("a{b}c", 9))
        self.assertEqual(read_group("{a\\}b}", 0), ("a\\}b", 6))
        self.assertIsNone(read_group("{open", 0))
        self.assertIsNone(read_group("x{a}", 0))
        self.assertEqual(read_optional("[a{]}b]{x}", 0), ("a{]}b", 7))
        t = "\\begin{itemize}a\\begin{itemize}b\\end{itemize}c\\end{itemize}d"
        self.assertEqual(find_env_end(t, "itemize", len("\\begin{itemize}")), (t.rindex("\\end"), t.rindex("d")))


class GathererTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, files, encoding="utf-8"):
        root = self.tmp / f"v{len(list(self.tmp.iterdir()))}"
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode(encoding))
        root.mkdir(exist_ok=True)
        return root

    def test_single_file(self):
        g = gather(self.folder({"paper.tex": DOC % "Hello world."}))
        self.assertEqual((g.status, g.root, g.files), (OK, "paper.tex", ["paper.tex"]))
        self.assertIn("Hello world.", g.text)

    def test_compilation_path(self):
        g = gather(self.folder({
            "main.tex": DOC % ("\\input{sections/intro}\n%\\input{old}\n\\iffalse\\input{draft}\\fi\n"
                               "\\begin{comment}\n\\input{x}\n\\end{comment}\n\\include{chap/ch1}\n"
                               "\\input sections/method.tex\nEND"),
            "sections/intro.tex": "INTRO \\input{sections/sub} \\input{fig}",
            "sections/sub.tex": "SUB", "sections/fig.tex": "FIG", "chap/ch1.tex": "CH1",
            "sections/method.tex": "METHOD", "old.tex": "OLD", "draft.tex": "DRAFT", "x.tex": "X",
            "orphan.tex": "ORPHAN"}))
        self.assertEqual(g.files, ["main.tex", "sections/intro.tex", "sections/sub.tex", "sections/fig.tex",
                                   "chap/ch1.tex", "sections/method.tex"])
        order = [g.text.find(w) for w in ("INTRO", "SUB", "FIG", "CH1", "METHOD", "END")]
        self.assertTrue(all(i >= 0 for i in order) and order == sorted(order))
        self.assertFalse(any(w in g.text for w in ("OLD", "DRAFT", "ORPHAN")))
        self.assertEqual(sorted(g.unused), ["draft.tex", "old.tex", "orphan.tex", "x.tex"])
        self.assertIn("\n\nCH1\n\n", g.text)

    def test_root_choice(self):
        g = gather(self.folder({
            "main.tex": DOC % "\\input{figs/plot} MAIN",
            "figs/plot.tex": "\\documentclass{standalone}\n\\begin{document}PLOT\\end{document}",
            "letter.tex": "\\documentclass{letter}\n\\begin{document}Dear editor\\end{document}"}))
        self.assertEqual(g.root, "main.tex")
        self.assertIn("PLOT", g.text)
        self.assertNotIn("\\documentclass{standalone}", g.text)
        self.assertTrue(any("3 files with \\documentclass" in p for p in g.problems))
        g = gather(self.folder({"aaa_cover.tex": "\\documentclass{letter}\n\\begin{document}Dear\\end{document}",
                                "ms.tex": DOC % "\\input{body}", "body.tex": "BODY " * 50}))
        self.assertEqual(g.root, "ms.tex")
        g = gather(self.folder({"old.tex": "\\documentstyle[12pt]{article}\n\\begin{document}OLD\\end{document}",
                                "notes.tex": "notes"}))
        self.assertEqual(g.root, "old.tex")

    def test_roots_without_documentclass(self):
        g = gather(self.folder({"only.tex": "Just text."}))
        self.assertEqual(g.root, "only.tex")
        g = gather(self.folder({"a.tex": "\\begin{document}A\\end{document}", "b.tex": "B" * 10}))
        self.assertEqual(g.root, "a.tex")
        self.assertTrue(any("guessed" in p for p in g.problems))

    def test_subfiles_import_and_includeonly(self):
        g = gather(self.folder({
            "main.tex": "\\documentclass{article}\\usepackage{subfiles}\n\\begin{document}\n\\subfile{sec/a}\n\\end{document}",
            "sec/a.tex": "\\documentclass[../main.tex]{subfiles}\n\\begin{document}\nA BODY\n\\end{document}\n"}))
        self.assertIn("A BODY", g.text)
        self.assertNotIn("{subfiles}\n\\begin{document}\nA", g.text)
        g = gather(self.folder({"main.tex": DOC % "\\import{chapters/}{intro}",
                                "chapters/intro.tex": "INTRO \\subimport{parts/}{p1}",
                                "chapters/parts/p1.tex": "PART1"}))
        self.assertEqual(g.files[-1], "chapters/parts/p1.tex")
        g = gather(self.folder({
            "main.tex": "\\documentclass{book}\\includeonly{ch1}\n\\begin{document}\\include{ch1}\\include{ch2}\\end{document}",
            "ch1.tex": "ONE", "ch2.tex": "TWO"}))
        self.assertIn("ONE", g.text)
        self.assertNotIn("TWO", g.text)

    def test_cycles_missing_and_endinput(self):
        g = gather(self.folder({"a.tex": DOC % "A \\input{b}", "b.tex": "B \\input{a}"}))
        self.assertTrue(any("cycle" in p for p in g.problems))
        self.assertEqual(g.text.count("B "), 1)
        g = gather(self.folder({"main.tex": DOC % "\\input{nothere} \\input{../outside} T"}))
        self.assertEqual(g.missing, ["nothere", "../outside"])
        g = gather(self.folder({"main.tex": DOC % "\\input{ch}", "ch.tex": "KEPT \\endinput same line\nDROPPED"}))
        self.assertIn("same line", g.text)
        self.assertNotIn("DROPPED", g.text)
        g = gather(self.folder({"main.tex": "\\documentclass{article}\\newcommand{\\inc}[1]{\\input{#1}}\n"
                                            "\\begin{document}\\inc{a}\\end{document}", "a.tex": "A"}))
        self.assertEqual(g.missing, [])
        self.assertTrue(any("names a macro" in p for p in g.problems))

    def test_bibliography(self):
        g = gather(self.folder({"main.tex": DOC % "\\bibliography{refs,gone}", "refs.bib": "@article{a,}"}))
        self.assertEqual((g.bib_files, g.bib_names_missing), (["refs.bib"], ["gone"]))
        g = gather(self.folder({"main.tex": DOC % "\\cite{x} \\bibliography{lost}"}))
        self.assertTrue(any(".bbl" in p for p in g.problems))
        g = gather(self.folder({"main.tex": "\\documentclass{article}\\addbibresource[label=x]{lib/refs.bib}\n"
                                            "\\begin{document}\\printbibliography\\end{document}",
                                "lib/refs.bib": "@book{b,}"}))
        self.assertEqual(g.bib_files, ["lib/refs.bib"])

    def test_versions_without_source_and_odd_files(self):
        self.assertEqual(gather(self.folder({}), status="pdf_only").status, NO_SOURCE)
        self.assertEqual(gather(self.folder({})).status, NO_TEX)
        self.assertEqual(gather(self.tmp / "nope").status, NO_SOURCE)
        g = gather(self.folder({"main.tex": DOC % "Caf\u00e9"}, encoding="latin-1"))
        self.assertIn("Caf\u00e9", g.text)
        g = gather(self.folder({"main.tex": DOC % "\\input{Intro}", "intro.tex": "LOWER"}))
        self.assertIn("LOWER", g.text)
        self.assertTrue(any("case differs" in p for p in g.problems))
        g = gather(self.folder({"main.tex": DOC % "\\InputIfFileExists{extra}{\\message{y}}{\\message{n}} AFTER",
                                "extra.tex": "EXTRA"}))
        self.assertIn("EXTRA", g.text)
        self.assertNotIn("\\message", g.text)


class HardSourceTest(unittest.TestCase):
    """What real sources do: each of these once gave a wrong tree."""

    def gather(self, files):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for rel, text in files.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return gather(tmp)

    def headings(self, body):
        reg, root, tree = tree_of(body)
        rows = {r["element_id"]: r for r in reg.rows()}
        return [rows[c]["content"] for c, _ in tree.edges if rows[c]["kind"] in ("heading", "paragraph")], reg, tree

    def test_math_cannot_hide_sections(self):
        for text in ("$a$$b$ here", "a lone $$ pair", "a lone \\[ bracket"):
            self.assertEqual(self.headings(f"\\section{{One}}\n{text}.\n\\section{{Two}}\nB.\n\\section{{Three}}\nC.")[0],
                             ["One", "Two", "Three"], text)

    def test_commands_that_are_text_or_definitions(self):
        self.assertEqual(self.headings("\\section{One}\n\\verb|\\section{F}| \\lstinline|\\section{G}| x.")[0], ["One"])
        self.assertEqual(self.headings("\\newcommand{\\f}{\\section{F}}\\def\\g#1{\\subsection{#1}}\n\\section{One}\nA.")[0], ["One"])
        self.assertEqual(self.headings("\\newcommand{\\be}{\\begin{equation}}\n\\section{One}\nA.\n"
                                       "\\section{Two}\n\\begin{equation}x\\end{equation}")[0], ["One", "Two"])

    def test_no_reference_reaches_the_tree(self):
        for tail in ("\\section*{References}\n[1] Z. Author, title.\n\n[2] Z. Author, other.",
                     "\\section*{Bibliography}\\begin{itemize}\\item Z. Author.\\end{itemize}",
                     "\\begin{thebibliography}{9}\\bibitem{a} Z. Author.",
                     "\\begin{references}\nZ. Author.\n\\end{references}"):
            heads, reg, _ = self.headings("\\section{One}\nA.\n" + tail)
            self.assertEqual(heads, ["One"], tail)
            self.assertFalse(any("Author" in r["content"] for r in reg.rows()), tail)
        heads, reg, _ = self.headings("\\section*{References}\nZ. Author.\n\\appendix\n\\section{Proof}\nP.")
        self.assertEqual(heads, ["Appendix", "Proof"])

    def test_abstract_appendices_and_macros(self):
        self.assertEqual(self.headings("\\abstract{We study.}\n\\section{One}\nA.")[0], ["Abstract", "One"])
        self.assertEqual(self.headings("\\begin{appendices}\n\\section{P}\nA.\n\\end{appendices}")[0], ["Appendix", "P"])
        reg, root, tree = tree_of("\\sect{One}\nA.\n\\ssect{Two}\nB.",
                                  preamble="\\documentclass{a}\\newcommand{\\sect}[1]{\\section{#1}}\\def\\ssect{\\subsection}")
        self.assertEqual([r["content"] for r in reg.rows() if r["kind"] == "heading"], ["One", "Two"])
        self.assertTrue(any("no heading" in p for p in self.headings("Only text here.")[2].problems))

    def test_root_is_what_includes_the_documentclass(self):
        g = self.gather({"main.tex": "\\input{header}\n\\begin{document}\nA.\n\\end{document}",
                         "header.tex": "\\documentclass{article}", "fig.tex": "\\documentclass{standalone}\\begin{document}F\\end{document}"})
        self.assertEqual(g.root, "main.tex")
        g = self.gather({"all.tex": "\\input{a}\\input{b}", "a.tex": "\\documentclass{article}", "b.tex": "\\begin{document}A."})
        self.assertEqual(g.root, "all.tex")

    def test_names_through_macros_and_literal_includes(self):
        g = self.gather({"m.tex": "\\documentclass{a}\\newcommand{\\dir}{s}\\begin{document}\\input{\\dir/x}\\bibliography{\\jobname}"
                                  "\\verb|\\input{y}|\\end{document}", "s/x.tex": "XX", "m.bib": "@misc{a,}", "y.tex": "YY"})
        self.assertIn("XX", g.text)
        self.assertNotIn("YY", g.text)
        self.assertEqual(g.bib_files, ["m.bib"])

    def test_bbl_input_and_embedded_bib(self):
        g = self.gather({"m.tex": DOC % "\\input{m.bbl}", "old.bib": "@misc{a,}"})
        self.assertEqual((g.bib_files, g.bib_fallback, g.missing), ([], False, []))
        self.assertTrue(any(".bbl" in p for p in g.problems))
        g = self.gather({"m.tex": "\\begin{filecontents}{r.bib}\n@misc{a, note={5% off}}\n\\end{filecontents}\n" + DOC % "\\bibliography{r}"})
        self.assertIn("5% off", g.embedded_bib["r.bib"])
        self.assertEqual(g.bib_names_missing, [])

    def test_encodings_and_comments(self):
        for raw in (b"\\documentclass{a}\r\\begin{document}\r% no\r\\section{One}\rA.\r\\end{document}\r",
                    (DOC % "\\section{One}\nA.").encode("utf-16")):
            g = self.gather({"m.tex": raw})
            self.assertIn("\\section{One}", g.text)
            self.assertNotIn("% no", g.text)
        g = self.gather({"m.tex": DOC % "\\url{http://a.org/x%20y} \\href{http://b.org/%41}{t}"})
        self.assertIn("x%20y}", g.text)
        self.assertIn("%41}", g.text)


class BibFilesTest(unittest.TestCase):
    def gather(self, files):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for rel, text in files.items():
            (tmp / rel).write_text(text, encoding="utf-8")
        return gather(tmp)

    def test_fallback_only_when_nothing_is_declared(self):
        g = self.gather({"main.tex": DOC % "No references declared.", "a.bib": "@misc{a,}"})
        self.assertEqual((g.bib_files, g.bib_fallback), (["a.bib"], True))
        self.assertTrue(any("fallback" in p for p in g.problems))
        g = self.gather({"main.tex": DOC % "\\bibliography{a}", "a.bib": "@misc{a,}", "old.bib": "@misc{o,}"})
        self.assertEqual((g.bib_files, g.bib_fallback), (["a.bib"], False))
        g = self.gather({"main.tex": DOC % "\\bibliography{gone}", "old.bib": "@misc{o,}"})
        self.assertEqual((g.bib_files, g.bib_fallback), ([], False))
        g = self.gather({"main.tex": DOC % "\\begin{thebibliography}{9}\\end{thebibliography}", "old.bib": "@misc{o,}"})
        self.assertEqual((g.bib_files, g.bib_fallback), ([], False))


def tree_of(body, registry=None, version=1, preamble="\\documentclass{article}\nPREAMBLE TEXT.\n"):
    registry = registry or BasicRegistry("2605.00001")
    root = registry.element_id("document", f"Version {version} root", None, 0)
    tree = build(preamble + "\\begin{document}\n" + body + "\n\\end{document}\n", root, registry,
                 extract_leaves_basic)
    return registry, root, tree


def children(registry, tree):
    rows = {r["element_id"]: r for r in registry.rows()}
    out = defaultdict(list)
    for child, parent in tree.edges:
        out[parent].append((rows[child]["kind"], rows[child]["content"]))
    return rows, out


def assert_tree(test, registry, root, tree):
    """Every parent known, every node reaches the root, no cycle."""
    rows = {r["element_id"] for r in registry.rows()}
    parents = defaultdict(set)
    for child, parent in tree.edges:
        test.assertIn(parent, rows)
        test.assertNotEqual(child, root)
        parents[child].add(parent)
    for child in parents:
        seen, todo = set(), [child]
        while todo:
            node = todo.pop()
            if node == root:
                continue
            test.assertNotIn(node, seen, "cycle")
            test.assertIn(node, parents, "orphan")
            seen.add(node)
            todo.extend(parents[node])


class HierarchyTest(unittest.TestCase):
    def test_sections_abstract_front_matter(self):
        reg, root, tree = tree_of("\\title{The Title}\\author{Ann}\\maketitle\n"
                                  "\\begin{abstract}\nWe study things. They are nice.\n\\end{abstract}\n"
                                  "\\section{Introduction}\\label{s}\nIntro one. Intro two.\n\nSecond paragraph.\n"
                                  "\\subsection{Background}\nBackground.\n\\paragraph{Note.} A note.\n"
                                  "\\section*{Method}\nMethod text.")
        assert_tree(self, reg, root, tree)
        rows, kids = children(reg, tree)
        self.assertEqual([c for _, c in kids[root]], ["Abstract", "Introduction", "Method"])
        heading = {r["content"]: e for e, r in rows.items()}
        self.assertEqual(kids[heading["Abstract"]], [("sentence", "We study things."), ("sentence", "They are nice.")])
        self.assertIn(("heading", "Background"), kids[heading["Introduction"]])
        self.assertIn(("paragraph", "Note."), kids[heading["Background"]])
        self.assertFalse(any("Title" in r["content"] or "PREAMBLE" in r["content"] or "Ann" == r["content"]
                             for r in reg.rows()))

    def test_appendix_and_acknowledgements(self):
        reg, root, tree = tree_of("\\section[Short]{A Long Title}\nText.\n\\appendix\n\\section{Proofs}\nP.\n"
                                  "\\subsection{Lemma}\nL.\n\\section*{Acknowledgments}\nThanks.\n"
                                  "\\section{More appendix}\nMore.\n"
                                  "\\begin{acknowledgements}\nWe thank X.\n\\end{acknowledgements}\n")
        assert_tree(self, reg, root, tree)
        rows, kids = children(reg, tree)
        self.assertEqual([c for _, c in kids[root]], ["A Long Title", "Appendix", "Acknowledgments", "Acknowledgements"])
        appendix = next(e for e, r in rows.items() if r["content"] == "Appendix")
        self.assertEqual([c for _, c in kids[appendix]], ["Proofs", "More appendix"])

    def test_lists(self):
        reg, root, tree = tree_of("\\section{Lists}\nBefore.\n\\begin{itemize}\n\\item First. Still first.\n"
                                  "\\item Second\n\\begin{enumerate}[(a)]\\item Nested one.\\item Nested two."
                                  "\\end{enumerate}\nback in second.\n\\end{itemize}\n"
                                  "\\begin{description}\\item[Term] Definition.\\end{description}\n"
                                  "\\begin{list}{--}{\\setlength{\\itemsep}{0pt}}\\item Generic.\\end{list}\n"
                                  "After lists.\n\\item stray.")
        assert_tree(self, reg, root, tree)
        rows, _ = children(reg, tree)
        kinds = [r["kind"] for r in reg.rows()]
        self.assertEqual((kinds.count("list"), kinds.count("item")), (4, 6))
        parent_kind = {rows[c]["content"]: rows[p]["kind"] for c, p in tree.edges if p in rows}
        self.assertEqual(parent_kind["Nested one."], "item")
        self.assertEqual(parent_kind["back in second."], "item")
        self.assertIn("Term", [r["content"] for r in reg.rows() if r["kind"] == "item"])
        self.assertFalse(any(r["content"] in ("(a)", "--") or "itemsep" in r["content"] for r in reg.rows()))

    def test_blocks_kept_whole_and_bibliography_removed(self):
        reg, root, tree = tree_of("\\section{Floats}\nBefore.\n\\begin{figure}[t]\n\\centering\n\n"
                                  "\\caption{A caption. Two sentences.}\n\\end{figure}\n$$\nx = 1\n\ny = 2\n$$\n"
                                  "then \\begin{align} a &= b \\\\ c &= d \\end{align} end. Costs \\$5 and \\$$ cheap.\n"
                                  "\\begin{table}\\begin{tabular}{cc} 1 & 2 \\end{tabular}\\end{table}\n"
                                  "\\section*{References}\n\\begin{thebibliography}{9}\\bibitem{a} A. Author.\n"
                                  "\\end{thebibliography}\n\\bibliographystyle{plain}\\bibliography{refs}")
        assert_tree(self, reg, root, tree)
        kinds = [r["kind"] for r in reg.rows()]
        self.assertEqual((kinds.count("figure"), kinds.count("formula")), (2, 2))
        self.assertFalse(any(r["content"] in ("References",) or "Author" in r["content"] for r in reg.rows()))

    def test_paragraphs_never_cut_inside_braces(self):
        self.assertEqual(split_paragraphs("a\\footnote{x.\n\ny.} b.\n\nc."), ["a\\footnote{x.\n\ny.} b.", "c."])
        self.assertEqual(split_paragraphs("a.\\par b. \\part"), ["a.", " b. \\part"])

    def test_same_content_twice(self):
        reg, root, tree = tree_of("\\section{A}\n\\paragraph{Proof.} One.\n\\paragraph{Proof.} Two.\nSame.\n"
                                  "\\section{B}\nSame.")
        self.assertEqual(len([r for r in reg.rows() if r["content"] == "Proof."]), 2)
        same = [r["element_id"] for r in reg.rows() if r["content"] == "Same."]
        self.assertEqual(len(same), 1)
        self.assertEqual(len([p for c, p in tree.edges if c == same[0]]), 2)

    def test_cross_version_ids(self):
        reg = BasicRegistry("2605.00001")
        _, r1, t1 = tree_of("\\section{Introduction} Same. Old.\n\\section{Method} Stays.", reg, 1)
        _, r2, t2 = tree_of("\\section{Introduction} Same. New.\n\\section{Method} Stays.\n\\section{X} Y.", reg, 2)
        ids = {r["content"]: r["element_id"] for r in reg.rows()}
        e1, e2 = {c for c, _ in t1.edges}, {c for c, _ in t2.edges}
        self.assertNotEqual(r1, r2)
        for shared in ("Same.", "Introduction", "Method", "Stays."):
            self.assertIn(ids[shared], e1 & e2)
        self.assertIn(ids["Old."], e1 - e2)
        self.assertIn(ids["New."], e2 - e1)

    def test_title_repeated_in_a_version_is_never_its_own_parent(self):
        reg, root, tree = tree_of("\\section{Results}\nOne.\n\\subsection{Results}\nTwo.\n"
                                  "\\section{A}\n\\subsection{Setup}\nThree.\n\\section{B}\n\\subsection{Setup}\nFour.")
        assert_tree(self, reg, root, tree)
        self.assertEqual(len([r for r in reg.rows() if r["content"] == "Results"]), 2)
        self.assertEqual(len([r for r in reg.rows() if r["content"] == "Setup"]), 2)
        self.assertFalse([e for e in tree.edges if e[0] == e[1]])

    def test_title_moved_or_parent_renamed_keeps_one_row(self):
        reg = BasicRegistry("2605.00001")
        _, _, t1 = tree_of("\\section{Intro}\n\\subsection{Goal}\nText one.\n\\section{B}\nText two.", reg, 1)
        _, _, t2 = tree_of("\\section{Introduction}\nText one.\n\\section{B}\n\\subsection{Goal}\nText two.", reg, 2)
        goal = [r["element_id"] for r in reg.rows() if r["content"] == "Goal"]
        self.assertEqual(len(goal), 1)
        ids = {r["content"]: r["element_id"] for r in reg.rows()}
        self.assertEqual(dict(t1.edges)[goal[0]], ids["Intro"])
        self.assertEqual(dict(t2.edges)[goal[0]], ids["B"])

    def test_lists_are_not_merged(self):
        reg, root, tree = tree_of("\\section{A}\n\\begin{itemize}\\item One.\\item Two.\\end{itemize}\n"
                                  "\\section{B}\n\\begin{itemize}\\item Three.\\end{itemize}")
        assert_tree(self, reg, root, tree)
        kinds = [r["kind"] for r in reg.rows()]
        self.assertEqual((kinds.count("list"), kinds.count("item")), (2, 3))

    def test_sentence_twice_under_one_parent_has_two_edges(self):
        reg, root, tree = tree_of("\\section{A}\nSame text.\n\nSame text.")
        same = [r["element_id"] for r in reg.rows() if r["content"] == "Same text."]
        self.assertEqual(len(same), 1)
        self.assertEqual(len([c for c, _ in tree.edges if c == same[0]]), 2)

    def test_malformed_input(self):
        reg, root, tree = tree_of("\\section{S}\n\\begin{itemize}\n\\item One\n\\section{Inside}\nText.\n"
                                  "\\end{itemize}\n\\begin{theorem}\nT.\n\\begin{enumerate}\\item Case.\\end{enumerate}\n"
                                  "\\end{theorem}\n\\section{Unbalanced { brace} text")
        assert_tree(self, reg, root, tree)
        self.assertTrue(any("inside an open" in p for p in tree.problems))
        reg, root, tree = tree_of("")
        self.assertEqual(tree.edges, [])


class PipelineTest(unittest.TestCase):
    def test_small_corpus(self):
        import pipeline_m2
        try:
            import pyarrow  # noqa: F401
            export = []
        except ImportError:
            export = ["--no-export"]
        tmp = Path(tempfile.mkdtemp())
        try:
            data = tmp / "data"
            papers = {"2605.00001": {"v1": ("success", {"main.tex": DOC % "\\section{A}Same. Old."}),
                                     "v2": ("success", {"main.tex": DOC % "\\section{A}Same. New."})},
                      "2605.00002": {"v1": ("pdf_only", {})}}
            (data / "work").mkdir(parents=True)
            with open(data / "work" / "paper_manifest.jsonl", "w", encoding="utf-8") as manifest:
                for arxiv_id, versions in papers.items():
                    paper = data / "corpus" / arxiv_id.replace(".", "-")
                    paper.mkdir(parents=True)
                    for version, (status, files) in versions.items():
                        for rel, text in files.items():
                            (paper / version).mkdir(parents=True, exist_ok=True)
                            (paper / version / rel).write_text(text, encoding="utf-8")
                    (paper / "versions.json").write_text(json.dumps(
                        {"versions": {v: {"status": s} for v, (s, _) in versions.items()}}))
                    manifest.write(json.dumps({"arxiv_id": arxiv_id, "status": "success",
                                               "versions": list(versions)}) + "\n")
            self.assertEqual(pipeline_m2.main(["--data-dir", str(data), "--workers", "1"] + export), 0)
            records = [json.loads(line) for line in (data / "logs" / "parse_manifest.jsonl").read_text().splitlines()]
            self.assertEqual([r["status"] for r in records], ["done", "done"])
            elements = json.loads((data / "corpus" / "2605-00001" / "parsed" / "elements.json").read_text(encoding="utf-8"))
            contents = [row[2] for row in elements["rows"]]
            self.assertEqual(contents.count("Same."), 1)
            self.assertIn("Version 2 root", contents)
            empty = json.loads((data / "corpus" / "2605-00002" / "parsed" / "hierarchy.json").read_text())
            self.assertEqual(empty["rows"], [])
            if not export:
                import pyarrow.parquet as pq
                table = pq.read_table(data / "tables" / "hierarchy" / "part-00000.parquet")
                self.assertFalse(table.schema.field("parent_id").nullable)
                self.assertEqual(table.num_rows, 6)  # v1: A, Same., Old.; v2: A, Same., New.
            self.assertEqual(pipeline_m2.main(["--data-dir", str(data), "--workers", "1", "--no-export"]), 0)
            if not export:
                self.assertEqual(pipeline_m2.main(["--data-dir", str(data), str(data), "--out", str(tmp / "t"),
                                                   "--export-only"]), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
