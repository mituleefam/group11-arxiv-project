"""Offline tests of the Milestone 2 Member 2 modules: cleaner, normalizer,
sentence_splitter and leaf_extractor, alone and inside hierarchy.build.

    python src/test_m2_member2.py

No network, no data folder.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import m2_contract  # noqa: E402
from cleaner import clean  # noqa: E402
from hierarchy import build  # noqa: E402
from leaf_extractor import extract_leaves  # noqa: E402
from m2_contract import BasicRegistry  # noqa: E402
from normalizer import math_spans, normalize  # noqa: E402
from sentence_splitter import split_sentences  # noqa: E402


class CleanerTest(unittest.TestCase):
    def test_statement_examples(self):
        text = ("\\begin{figure}[htpb]\n\\centering\n\\includegraphics{a.png}\n\\end{figure}\n"
                "\\begin{tabular}{cc}\\toprule\na & b \\\\ \\midrule\nc & d \\\\ \\bottomrule\n\\end{tabular}")
        out = clean(text)
        for gone in ("[htpb]", "\\centering", "\\toprule", "\\midrule", "\\bottomrule"):
            self.assertNotIn(gone, out)
        self.assertIn("\\begin{figure}\n\\includegraphics{a.png}", out)
        self.assertIn("a & b \\\\", out)  # the row break of the table stays

    def test_layout_commands(self):
        cases = {
            "\\noindent Text": "Text",
            "a \\hspace{2mm} b": "a b",
            "a\\vspace*{-3pt} b": "a b",
            "\\small small \\normalsize text": "small text",
            "a\\newline b": "a b",
            "x \\vskip 2mm plus 1mm y": "x y",
            "\\setlength{\\tabcolsep}{4pt}T": "T",
            "\\cline{2-3} \\cmidrule(lr){1-2} r": "r",
            "\\color[rgb]{1,0,0}red": "red",
            "\\clearpage\\newpage Next": "Next",
        }
        for source, expected in cases.items():
            self.assertEqual(clean(source).strip(), expected, source)

    def test_settings_written_in_the_body(self):
        text = ("\\hypersetup{ colorlinks, linkcolor=black, urlcolor={blue!80!black} }\n"
                "\\setcounter{page}{1}\\pagenumbering{arabic}\\graphicspath{{fig/}}\\linenumbers\n"
                "Real text stays.")
        out = clean(text)
        for gone in ("hypersetup", "colorlinks", "setcounter", "pagenumbering", "graphicspath", "linenumbers"):
            self.assertNotIn(gone, out)
        self.assertIn("Real text stays.", out)
        self.assertEqual(extract_leaves(normalize(out)), [("sentence", "Real text stays.")])

    def test_font_commands_keep_their_text(self):
        self.assertEqual(clean("\\textbf{bold \\emph{nested}} word"), "bold nested word")
        self.assertEqual(clean("an {\\bf old} and {\\em style}"), "an old and style")
        self.assertEqual(clean("\\textcolor{red}{x} \\resizebox{\\linewidth}{!}{y}"), "x y")
        # the braces of a command's argument stay: only the switch goes
        self.assertEqual(clean("\\caption{\\small A caption.}"), "\\caption{A caption.}")

    def test_spacing_characters(self):
        self.assertEqual(clean("Fig.~\\ref{f}\\@. A\\,B\\ C"), "Fig. \\ref{f}. A B C")
        self.assertEqual(clean("50\\% of \\{x\\}"), "50\\% of \\{x\\}")

    def test_layout_environments(self):
        out = clean("\\begin{center}\nmiddle\n\\end{center}\n\\begin{minipage}[t]{0.5\\textwidth}m\\end{minipage}")
        self.assertEqual(out, "middle\nm")

    def test_removed_line_makes_no_blank_line(self):
        self.assertEqual(clean("first line\n\\vspace{2mm}\nsecond line"), "first line\nsecond line")
        self.assertEqual(clean("one\n\n\\medskip\ntwo"), "one\n\ntwo")  # a real blank line stays

    def test_structure_and_semantics_kept(self):
        text = ("\\section{Intro}\\label{sec:intro}\n\\begin{abstract}A\\end{abstract}\n"
                "\\begin{itemize}\n\\item one \\cite{a,b}\n\\end{itemize}\n\\appendix\n"
                "\\begin{thebibliography}{9}\\bibitem{a} A.\\end{thebibliography}\\ref{x}\\footnote{f}")
        self.assertEqual(clean(text), text)

    def test_formulas_and_literals_untouched(self):
        for text in ("$a\\,b \\hspace{1em} \\textbf{x}$",
                     "\\begin{align}a &= b \\\\ \\midrule\\end{align}",
                     "\\url{http://x.org/~me}", "\\verb|\\centering|",
                     "\\begin{verbatim}\n\\vspace{1cm} ~\n\\end{verbatim}"):
            self.assertEqual(clean(text), text, text)


class NormalizerTest(unittest.TestCase):
    def test_inline(self):
        self.assertEqual(normalize("a \\(x+1\\) b"), "a $x+1$ b")
        self.assertEqual(normalize("\\ensuremath{\\alpha} and \\begin{math}y\\end{math}"), "$\\alpha$ and $y$")
        self.assertEqual(normalize("$ x $"), "$x$")

    def test_display(self):
        eq = "\\begin{equation}%s\\end{equation}"
        self.assertEqual(normalize("$$E=mc^2$$"), eq % "E=mc^2")
        self.assertEqual(normalize("\\[ y \\]"), eq % " y ")
        self.assertEqual(normalize("\\begin{equation*}z\\end{equation*}"), eq % "z")
        self.assertEqual(normalize("\\begin{displaymath}w\\end{displaymath}"), eq % "w")
        self.assertEqual(normalize("\\begin{equation}\\label{e}v\\end{equation}"), eq % "\\label{e}v")

    def test_multiline_environments_stay_valid(self):
        self.assertEqual(normalize("\\begin{align*}a &= b \\\\ c &= d\\end{align*}"),
                         "\\begin{equation}\\begin{aligned}a &= b \\\\ c &= d\\end{aligned}\\end{equation}")
        self.assertEqual(normalize("\\begin{eqnarray}a &=& b\\end{eqnarray}"),
                         "\\begin{equation}\\begin{aligned}a &=& b\\end{aligned}\\end{equation}")
        self.assertEqual(normalize("\\begin{alignat}{2}x &= 1\\end{alignat}"),
                         "\\begin{equation}\\begin{alignedat}{2}x &= 1\\end{alignedat}\\end{equation}")
        self.assertEqual(normalize("\\begin{gather}p \\\\ q\\end{gather}"),
                         "\\begin{equation}\\begin{gathered}p \\\\ q\\end{gathered}\\end{equation}")

    def test_wrapper_is_dropped(self):
        self.assertEqual(normalize("\\begin{subequations}\\begin{equation}a\\end{equation}"
                                   "\\begin{equation*}b\\end{equation*}\\end{subequations}"),
                         "\\begin{equation}a\\end{equation}\\begin{equation}b\\end{equation}")

    def test_not_math(self):
        for text in ("costs \\$5 and \\$6", "\\verb|$x$| \\url{a$b}", "\\begin{verbatim}\\[ x \\]\\end{verbatim}",
                     "a lone $ sign\n\nnext paragraph $", "$$ open\n\\section{S} $$"):
            self.assertEqual(normalize(text), text, text)

    def test_inline_next_to_inline(self):
        self.assertEqual(normalize("$a$$b$"), "$a$$b$")
        self.assertEqual([s.inner for s in math_spans("$a$$b$")], ["a", "b"])

    def test_idempotent(self):
        text = "\\[ a \\] \\(b\\) \\begin{align}c\\end{align}"
        once = normalize(text)
        self.assertEqual(normalize(once), once)


class SentenceSplitterTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(split_sentences("One sentence. Two sentences! Three? Four."),
                         ["One sentence.", "Two sentences!", "Three?", "Four."])

    def test_no_cut_inside_protected_text(self):
        for text in ("We use it, i.e. the model, and more, e.g. Fig. 1 shows it.",
                     "Smith et al. Proposed it in their work.",
                     "See Fig.~\\ref{fig.a} and Eq. 3 for details.",
                     "The value is 3.14 and 0.001 in v1.2 here.",
                     "Visit https://arxiv.org/abs/2604.05535v1. Now and mail me@uni.edu. Ok.",
                     "As in \\cite{smith.2020, Doe. X} We see it.",
                     "Let $x. Y$ be a set and \\(a.b\\) too.",
                     "The DOI 10.1016/j.cell.2020. Is known.",
                     "Written by J. Smith and A. B. Jones today."):
            out = split_sentences(text)
            joined = " ".join(out)
            self.assertEqual(" ".join(joined.split()), " ".join(text.split()), text)
        self.assertEqual(len(split_sentences("Smith et al. Proposed it in their work.")), 1)
        self.assertEqual(len(split_sentences("The value is 3.14 and 0.001 in v1.2 here.")), 1)
        self.assertEqual(len(split_sentences("See Fig.~\\ref{fig.a} and Eq. 3 for details.")), 1)

    def test_url_then_sentence(self):
        self.assertEqual(split_sentences("See \\url{http://a.b/c.d}. Next one."),
                         ["See \\url{http://a.b/c.d}.", "Next one."])

    def test_etc_ends_a_sentence(self):
        self.assertEqual(split_sentences("Cats, dogs, etc. They all live."),
                         ["Cats, dogs, etc.", "They all live."])

    def test_after_formula_and_footnote(self):
        self.assertEqual(split_sentences("It holds for $x>0$. Then $y$ follows."),
                         ["It holds for $x>0$.", "Then $y$ follows."])
        self.assertEqual(split_sentences("This is known.\\footnote{See it. Twice.} Another one."),
                         ["This is known.\\footnote{See it. Twice.}", "Another one."])

    def test_quotes_and_brackets(self):
        self.assertEqual(split_sentences('He said "stop." Then (it ended.) Done.'),
                         ['He said "stop."', "Then (it ended.)", "Done."])

    def test_empty_pieces_dropped(self):
        self.assertEqual(split_sentences("\\label{sec:x}  "), [])
        self.assertEqual(split_sentences(" . "), [])
        self.assertEqual(split_sentences("A\n  sentence   on lines."), ["A sentence on lines."])


class LeafExtractorTest(unittest.TestCase):
    def test_kinds_in_reading_order(self):
        paragraph = ("We define it. \\begin{equation}a=b\\end{equation} where $a$ is real. "
                     "\\begin{table}\\caption{T. x.}\\begin{tabular}{c}1\\end{tabular}\\end{table} Done.")
        leaves = extract_leaves(paragraph)
        self.assertEqual([k for k, _ in leaves], ["sentence", "formula", "sentence", "figure", "sentence"])
        self.assertEqual(leaves[1][1], "\\begin{equation}a=b\\end{equation}")
        self.assertTrue(leaves[3][1].startswith("\\begin{table}") and leaves[3][1].endswith("\\end{table}"))

    def test_display_math_is_normalized(self):
        self.assertEqual(extract_leaves("Let \\[ x \\] be."),
                         [("sentence", "Let"), ("formula", "\\begin{equation} x \\end{equation}"),
                          ("sentence", "be.")])

    def test_formula_inside_figure_stays_in_it(self):
        leaves = extract_leaves("\\begin{figure}\\begin{equation}a\\end{equation}\\caption{C}\\end{figure}")
        self.assertEqual([k for k, _ in leaves], ["figure"])

    def test_every_optional_argument_of_an_environment_goes(self):
        for text in ("\\begin{lemmaE}[][end,restate]\\label{a} Suppose it holds.\\end{lemmaE}",
                     "\\begin{lemma}[Informal]\\label{a} Suppose it holds.\\end{lemma}"):
            self.assertEqual(extract_leaves(normalize(clean(text))), [("sentence", "\\label{a} Suppose it holds.")])
        text = "\\begin{assumption}[LICQ \\citep{aragon2019}]\\label{ass} The gradients are independent.\\end{assumption}"
        self.assertEqual(extract_leaves(normalize(clean(text))),
                         [("sentence", "\\citep{aragon2019} \\label{ass} The gradients are independent.")])

    def test_other_figures(self):
        kinds = [k for k, _ in extract_leaves("Code: \\begin{verbatim}x. Y.\\end{verbatim} "
                                              "\\includegraphics[width=2cm]{a.png} and "
                                              "\\begin{tikzpicture}\\draw (0,0);\\end{tikzpicture}")]
        self.assertEqual(kinds, ["sentence", "figure", "figure", "sentence", "figure"])

    def test_read_through_environments(self):
        leaves = extract_leaves("\\begin{theorem}[Main] It holds. \\end{theorem} "
                                "Since $\\begin{array}{c} x \\end{array}$ is so.")
        self.assertEqual(leaves, [("sentence", "It holds."),
                                  ("sentence", "Since $\\begin{array}{c} x \\end{array}$ is so.")])


DOC = """\\documentclass{article}
\\begin{document}
\\begin{abstract}
We study \\textbf{matching}, i.e. a task. It is hard.
\\end{abstract}
\\section{Introduction}
\\noindent See Fig.~\\ref{fig:a} and \\cite{smith.2020}.
\\vspace{2mm}
Values like 3.14 help. Smith et al. Showed it.
\\begin{figure}[htbp]
\\centering
\\includegraphics{a.png}
\\caption{\\small A plot.}
\\end{figure}
\\subsection{Model}
\\begin{align*}
y &= x \\\\
z &= w
\\end{align*}
\\begin{itemize}
\\item First point. Second sentence.
\\end{itemize}
\\section*{Acknowledgements}
We thank you.
\\begin{thebibliography}{9}
\\bibitem{smith.2020} J. Smith. A paper. 2020.
\\end{thebibliography}
\\end{document}
"""


class PipelineTest(unittest.TestCase):
    def test_contract_loads_member2(self):
        comp = m2_contract.load()
        for module in ("cleaner", "normalizer", "leaf_extractor"):
            self.assertFalse(any(s.startswith(module + ".") for s in comp.stand_ins), comp.stand_ins)
        self.assertIs(comp.extract_leaves, extract_leaves)

    def test_tree_of_a_document(self):
        registry = BasicRegistry("2604.05535")
        tree = build(normalize(clean(DOC)), "2604-05535#d1", registry, extract_leaves)
        rows = {r["element_id"]: r for r in registry.rows()}
        kinds = [rows[e]["kind"] for e, _ in tree.edges]
        contents = [rows[e]["content"] for e, _ in tree.edges]
        self.assertIn("formula", kinds)
        self.assertIn("figure", kinds)
        self.assertIn("Acknowledgements", contents)
        self.assertIn("We study matching, i.e. a task.", contents)
        self.assertIn("See Fig. \\ref{fig:a} and \\cite{smith.2020}.", contents)
        self.assertIn("Smith et al. Showed it.", contents)
        self.assertIn("\\begin{equation}\\begin{aligned} y &= x \\\\ z &= w \\end{aligned}\\end{equation}", contents)
        self.assertTrue(all("\\centering" not in c and "[htbp]" not in c and "\\small" not in c
                            for c in contents))
        self.assertFalse(any("A paper. 2020." in c for c in contents))  # the bibliography is not in the tree
        figure = next(c for c in contents if c.startswith("\\begin{figure}"))
        self.assertEqual(figure, "\\begin{figure} \\includegraphics{a.png} \\caption{A plot.} \\end{figure}")


if __name__ == "__main__":
    unittest.main()
