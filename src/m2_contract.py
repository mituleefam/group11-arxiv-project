"""Milestone 2: what pipeline_m2.py calls in every member's module, and
minimal stand-ins used while a module is not in src/ yet.

The pipeline only goes through these functions, so each member can write
their module alone and the whole pipeline runs at every stage of the work:
load() takes the member's function when the module and the function exist,
the stand-in otherwise, and says which (pipeline_m2.py prints it). The
stand-ins are deliberately simple: they keep the tables valid, they are not
the members' work.

Member 2 (text):
    cleaner.clean(text: str) -> str
        the whole document (gatherer.Gathered.text) without formatting
        commands; sections, lists, \\cite, \\ref, \\label, figure and table
        environments are kept.
    normalizer.normalize(text: str) -> str
        inline math as $...$, display math as \\begin{equation}...\\end{equation}.
    leaf_extractor.extract_leaves(text: str) -> list[tuple[str, str]]
        one paragraph of the document (hierarchy.py cuts the text at
        sections, lists, items and blank lines); returns its leaves in
        reading order as (kind, content) with kind "sentence", "formula" or
        "figure" (tables are figures). The paragraph can hold display math
        and floats; it holds no section and no list.

Member 3 (references), on each version's gatherer.Gathered:
    bibitem_parser.parse(text: str, bib_files: dict[str, str]) -> list[dict]
        text: the whole document (comments already removed); bib_files: the
        content of the .bib files it names, by relative path.
    bibtex_converter.convert(entries: list[dict]) -> list[dict]
        rows with BIBTEX_FIELDS (arxiv_id is added by the pipeline).
    dedup_bibtex.dedup(by_version: dict[int, list[dict]]) -> tuple[list[dict], dict[str, str]]
        one row per distinct reference of the paper, fields unionized, and
        the map old citation key -> canonical key.
    citation_updater.update(text: str, rename: dict[str, str]) -> str
        the text with every \\cite-like key renamed.

Member 4 (element IDs):
    dedup_elements.Registry(arxiv_id: str)
        .element_id(kind, content, parent_id, occurrence) -> str
            the ID of an element of this paper, the same for the same
            element in every version. A leaf (sentence, formula, figure) is
            the same element wherever its content is the same, even twice in
            one version (several edges in `hierarchy`). A heading or paragraph
            (parent_id None) also needs the same occurrence: how many earlier
            nodes of this version have the same kind and content (0 for the
            first), so a title that moves between versions keeps its ID and
            one repeated in a version stays two nodes (never its own parent).
            A list or item has no content of its own, so it also needs the
            same place: parent_id is the parent's ID (None under the version's
            root, as roots differ per version) and occurrence counts the
            earlier siblings with the same kind and content.
        .rows() -> list[dict]   one row per element: ELEMENT_FIELDS
"""

from __future__ import annotations

import importlib
import re
from collections import Counter
from dataclasses import dataclass, field

ELEMENT_FIELDS = ["element_id", "arxiv_id", "kind", "content"]
HIERARCHY_FIELDS = ["arxiv_id", "version", "element_id", "parent_id"]
BIBTEX_FIELDS = ["arxiv_id", "citation_key", "entry_type", "title", "authors", "year", "doi",
                 "journal", "url"]
LEAF_KINDS = ("sentence", "formula", "figure")


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------

def same_text(text: str) -> str:
    return text


FLOAT_ENVS = ("figure", "figure*", "table", "table*", "wrapfigure", "wraptable", "sidewaysfigure",
              "sidewaystable", "SCfigure", "SCtable", "algorithm", "algorithm*", "longtable",
              "longtable*")
MATH_ENVS = ("equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*",
             "flalign", "flalign*", "alignat", "alignat*", "eqnarray", "eqnarray*", "displaymath",
             "subequations", "dmath", "dmath*", "dgroup", "dgroup*")
_ENV_NAMES = "|".join(re.escape(e) for e in sorted(FLOAT_ENVS + MATH_ENVS, key=len, reverse=True))
_DISPLAY = re.compile(r"\\begin\s*\{(" + _ENV_NAMES + r")\}|\\\[|(?<!\\)\$\$")
_ABBREVIATIONS = ("e.g.", "i.e.", "et al.", "etc.", "cf.", "vs.", "Fig.", "Figs.", "Eq.", "Eqs.", "Ref.",
                  "Refs.", "Sec.", "Secs.", "Tab.", "Thm.", "Lem.", "Prop.", "Def.", "Dr.", "Prof.",
                  "Mr.", "Ms.", "No.", "no.", "vol.", "pp.", "approx.", "resp.", "w.r.t.", "a.k.a.")
_PROTECT = re.compile(
    r"(?<!\\)\$[^$]*(?<!\\)\$"                       # inline math
    r"|\\\(.*?\\\)"                                  # \( ... \)
    r"|\\[A-Za-z@]+\*?(?:\[[^\]]*\])?\{[^{}]*\}"      # \cite{a.b}, \ref{sec.1}, \url{x.y}
    r"|https?://\S+|www\.\S+"                         # URLs
    r"|\d+\.\d+"                                      # 3.14, v1.2
    r"|" + "|".join(re.escape(a) for a in _ABBREVIATIONS), re.S)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\\(\[\"'`])")
_LETTER = re.compile(r"[A-Za-z]")
_SAVED = re.compile("\x01(\\d+)\x01")
# a command with its arguments: a piece made only of them (\vspace{2mm}, \label{x}) is no sentence
_COMMAND = re.compile(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])*(?:\{[^{}]*\})*")


def split_sentences(text: str) -> list[str]:
    """Stand-in sentence splitter: cuts after . ! ? followed by a capital,
    never inside inline math, a command argument, a URL, a decimal number or
    a few usual abbreviations. Pieces without a letter outside commands and
    their arguments (\\vspace{2mm}, a lone \\label{x}) are dropped."""
    saved: list[str] = []

    def keep(m):
        saved.append(m.group(0))
        return f"\x01{len(saved) - 1}\x01"

    masked = _PROTECT.sub(keep, text)
    out = []
    for piece in _SENTENCE_END.split(masked):
        if "\x01" in piece:
            piece = _SAVED.sub(lambda m: saved[int(m.group(1))], piece)
        piece = piece.strip()
        if _LETTER.search(_COMMAND.sub("", piece)):
            out.append(piece)
    return out


def extract_leaves_basic(text: str) -> list[tuple[str, str]]:
    """Stand-in for Member 2's leaf_extractor: floats are figures, display
    math is a formula, the text around them is split into sentences."""
    leaves: list[tuple[str, str]] = []
    pos = 0
    while True:
        m = _DISPLAY.search(text, pos)
        if m is None:
            break
        if m.group(1):
            close = re.compile(r"\\(begin|end)\s*\{" + re.escape(m.group(1)) + r"\}")
            depth, end = 1, None
            for c in close.finditer(text, m.end()):
                depth += 1 if c.group(1) == "begin" else -1
                if depth == 0:
                    end = c.end()
                    break
            kind = "figure" if m.group(1) in FLOAT_ENVS else "formula"
        else:
            closer = "\\]" if m.group(0) == "\\[" else "$$"
            found = text.find(closer, m.end())
            end = None if found < 0 else found + len(closer)
            kind = "formula"
        if end is None:  # not closed: the rest is text
            break
        leaves += [("sentence", s) for s in split_sentences(text[pos:m.start()])]
        leaves.append((kind, text[m.start():end].strip()))
        pos = end
    leaves += [("sentence", s) for s in split_sentences(text[pos:])]
    return leaves


def no_references(text: str, bib_files: dict[str, str]) -> list[dict]:
    return []


def same_entries(entries: list[dict]) -> list[dict]:
    return entries


def dedup_none(by_version: dict[int, list[dict]]) -> tuple[list[dict], dict[str, str]]:
    """Stand-in: rows of every version, the first of each citation key kept."""
    seen, rows = set(), []
    for version in sorted(by_version):
        for row in by_version[version]:
            if row.get("citation_key") not in seen:
                seen.add(row.get("citation_key"))
                rows.append(row)
    return rows, {}


_CITE = re.compile(r"(\\[A-Za-z]*cite[A-Za-z]*\*?(?:\[[^\]]*\]){0,2}\{)([^{}]*)\}")


def rename_citations(text: str, rename: dict[str, str]) -> str:
    """Stand-in: renames the keys of \\cite-like commands."""
    if not rename:
        return text
    return _CITE.sub(lambda m: m.group(1) + ",".join(rename.get(k.strip(), k.strip())
                                                     for k in m.group(2).split(",")) + "}", text)


PREFIX = {"document": "d", "heading": "h", "paragraph": "p", "list": "l", "item": "i",
          "sentence": "s", "formula": "f", "figure": "g"}


class BasicRegistry:
    """Stand-in for Member 4's dedup_elements.Registry (same contract)."""

    def __init__(self, arxiv_id: str):
        self.arxiv_id = arxiv_id
        self.prefix = arxiv_id.replace(".", "-").replace("/", "-")
        self._ids: dict[tuple, str] = {}
        self._rows: list[dict] = []
        self._count: Counter = Counter()

    def element_id(self, kind: str, content: str, parent_id: str | None, occurrence: int) -> str:
        content = " ".join(content.split())
        key = (kind, content) if kind in LEAF_KINDS else (kind, content, parent_id, occurrence)
        found = self._ids.get(key)
        if found is None:
            letter = PREFIX.get(kind, "x")
            self._count[letter] += 1
            found = self._ids[key] = f"{self.prefix}#{letter}{self._count[letter]}"
            self._rows.append({"element_id": found, "arxiv_id": self.arxiv_id, "kind": kind,
                               "content": content})
        return found

    def rows(self) -> list[dict]:
        return list(self._rows)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

@dataclass
class Components:
    clean: object = same_text
    normalize: object = same_text
    extract_leaves: object = extract_leaves_basic
    parse_references: object = no_references
    convert_references: object = same_entries
    dedup_references: object = dedup_none
    update_citations: object = rename_citations
    Registry: object = BasicRegistry
    stand_ins: list[str] = field(default_factory=list)  # "module.function" replaced by a stand-in


SLOTS = [  # (attribute, module, name in the module, owner)
    ("clean", "cleaner", "clean", "Member 2"),
    ("normalize", "normalizer", "normalize", "Member 2"),
    ("extract_leaves", "leaf_extractor", "extract_leaves", "Member 2"),
    ("parse_references", "bibitem_parser", "parse", "Member 3"),
    ("convert_references", "bibtex_converter", "convert", "Member 3"),
    ("dedup_references", "dedup_bibtex", "dedup", "Member 3"),
    ("update_citations", "citation_updater", "update", "Member 3"),
    ("Registry", "dedup_elements", "Registry", "Member 4"),
]


def load(use_modules: bool = True) -> Components:
    """The members' functions where their module is in src/, stand-ins
    elsewhere (all stand-ins with use_modules=False)."""
    components = Components()
    for attribute, module_name, name, owner in SLOTS:
        function = None
        if use_modules:
            try:
                function = getattr(importlib.import_module(module_name), name, None)
            except ModuleNotFoundError as exc:
                if exc.name != module_name:
                    raise  # the module exists but one of its own imports is missing
        if function is None:
            components.stand_ins.append(f"{module_name}.{name} ({owner})")
        else:
            setattr(components, attribute, function)
    return components
