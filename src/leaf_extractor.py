"""Member 2 - Leaf extraction (Milestone 2).

    extract_leaves(paragraph) -> [(kind, content), ...]

hierarchy.py cuts each version's document (already cleaned and normalized)
at headings, lists, items and blank lines, and calls this function on every
paragraph. The leaves come back in reading order, with the statement's three
kinds of smallest element:

    figure    a float: figure, table (tables are figures), algorithm, their
              starred, wrap and sideways forms; a tabular, tikzpicture,
              algorithmic, verbatim or code listing standing outside a float;
              an \\includegraphics standing alone. Content: the whole
              environment, \\caption and \\label included.
    formula   a display formula, \\begin{equation}...\\end{equation} as the
              normalizer writes it (the paragraph is normalized again here,
              which changes nothing in normalized text, so the function also
              works on text that was not).
    sentence  the prose around them, cut by sentence_splitter.split_sentences.

The \\begin and \\end (with its optional arguments) of an environment that
hierarchy.py reads through (theorem, proof, remark, quote...) are dropped from
the prose; their text stays, and so do the \\cite, \\ref and \\label written in
those optional arguments (\\begin{assumption}[LICQ \\citep{a}] keeps \\citep{a}). A paragraph holds no section and no list.
"""

from __future__ import annotations

import re

from latex_scan import find_env_end
from normalizer import math_spans, normalize
from sentence_splitter import command_end, split_sentences

FLOAT_ENVS = {"figure", "figure*", "table", "table*", "wrapfigure", "wraptable", "wrapfloat",
              "sidewaysfigure", "sidewaysfigure*", "sidewaystable", "sidewaystable*", "SCfigure", "SCtable",
              "algorithm", "algorithm*", "longtable", "longtable*", "subfigure", "subtable", "figwindow",
              "listing", "listing*"}
STANDALONE_ENVS = {"tabular", "tabular*", "tabularx", "tabulary", "tabu", "xtabular", "supertabular",
                   "tikzpicture", "tikzcd", "pgfpicture", "picture", "forest", "algorithmic", "algorithmic*",
                   "algorithm2e", "verbatim", "verbatim*", "Verbatim", "Verbatim*", "BVerbatim", "LVerbatim",
                   "spverbatim", "lstlisting", "minted", "alltt"}
FIGURE_ENVS = FLOAT_ENVS | STANDALONE_ENVS

_NAMES = "|".join(re.escape(e) for e in sorted(FIGURE_ENVS, key=len, reverse=True))
_FIGURE = re.compile(r"\\begin\s*\{(" + _NAMES + r")\}|\\includegraphics\*?(?![A-Za-z@])")
_ENV_MARK = re.compile(r"\\(begin|end)\s*\{[^{}]+\}(?:\s*\[[^\]\n]*\])*")
_KEPT_IN_OPTIONS = re.compile(r"\\(?:[A-Za-z]*cite[A-Za-z]*|ref|eqref|autoref|cref|Cref|pageref|nameref|label)"
                              r"\*?(?:\[[^\]]*\])*\{[^{}]*\}")


def _env_mark(m: re.Match) -> str:
    """What stays of an environment's \\begin / \\end: the \\cite, \\ref and \\label of its options."""
    kept = _KEPT_IN_OPTIONS.findall(m.group(0))
    return f" {' '.join(kept)} " if kept else " "


def _figure_spans(text: str, skip: list) -> list[tuple[int, int]]:
    """(start, end) of the outermost figure blocks, none inside a formula or a
    literal part (the spans of `skip`)."""
    spans, pos, k = [], 0, 0
    for m in _FIGURE.finditer(text):
        if m.start() < pos:
            continue
        while k < len(skip) and skip[k].end <= m.start():
            k += 1
        if k < len(skip) and skip[k].start <= m.start():
            literal_env = skip[k].start == m.start() and skip[k].env == "literal"
            if not literal_env:
                continue  # inside a formula, \verb or \url (a verbatim environment is a figure)
        if m.group(1):
            close = find_env_end(text, m.group(1), m.end())
            if close is None:  # never closed: left to the prose
                continue
            end = close[1]
        else:
            end = command_end(text, m.end())
        spans.append((m.start(), end))
        pos = end
    return spans


def _prose(text: str) -> list[str]:
    """Sentences of the text between two blocks, without the \\begin / \\end of
    the environments read through (never inside a formula: $\\begin{array}...$)."""
    out, pos = [], 0
    for span in math_spans(text, literals=True):
        out.append(_ENV_MARK.sub(_env_mark, text[pos:span.start]))
        out.append(text[span.start:span.end])
        pos = span.end
    out.append(_ENV_MARK.sub(_env_mark, text[pos:]))
    return split_sentences("".join(out))


def extract_leaves(paragraph: str) -> list[tuple[str, str]]:
    """The leaves of one paragraph in reading order (module docstring)."""
    text = normalize(paragraph)
    spans = math_spans(text, literals=True)
    blocks = [(s, e, "figure") for s, e in _figure_spans(text, spans)]
    for span in spans:
        if span.display and not any(s <= span.start < e for s, e, _ in blocks):
            blocks.append((span.start, span.end, "formula"))
    blocks.sort()
    leaves: list[tuple[str, str]] = []
    pos = 0
    for start, end, kind in blocks:
        leaves += [("sentence", s) for s in _prose(text[pos:start])]
        leaves.append((kind, text[start:end].strip()))
        pos = end
    leaves += [("sentence", s) for s in _prose(text[pos:])]
    return leaves
