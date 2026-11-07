"""Member 3 - Milestone 2: the references of a .bbl file.

A .bbl is what BibTeX or biber wrote when the authors compiled their paper. A
submission can hold it instead of its .bib files, and then it is the only
source of the references (the instructor's answer: keep this source). It comes
in two forms:

    BibTeX    \\begin{thebibliography}{9} \\bibitem{key} ... \\end{thebibliography}
              The same text LaTeX prints, read by bibitem_parser like a
              bibliography written in a .tex file.
    biblatex  \\entry{key}{article}{} \\name{author}... \\field{title}{...} \\endentry
              Structured data, turned back into BibTeX entries for bibitem_parser.

    bbl_parts(text) -> (thebibliography text, BibTeX text)    either can be ""
"""

from __future__ import annotations

import re

from latex_scan import read_group, strip_comments

LIMIT = 500_000  # how far an argument is looked for

_THEBIB = re.compile(r"\\begin\s*\{thebibliography\}.*?(?:\\end\s*\{thebibliography\}|\Z)", re.S)
_ENTRY = re.compile(r"\\entry\s*(?=\{)")
_END_ENTRY = re.compile(r"\\endentry(?![A-Za-z@])")
_VERB = re.compile(r"\\verb\s*\{(\w+)\}(.*?)\\endverb(?![A-Za-z@])", re.S)
_VERB_LINE = re.compile(r"\\verb[ \t]?([^\n]*)")
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
_DATA = re.compile(r"\\(field|name|list)\s*(?:\[[^\]]*\])?\s*(?=\{)")
_NAME_PART = re.compile(r"\b(family|given|prefix|suffix)=(?=\{)")
_NAME_DELIMITER = re.compile(r"\\bib(?:namedelim[a-z]|initdelim)(?![A-Za-z@])\s*")
_HYPHEN = re.compile(r"\\bibinithyphendelim(?![A-Za-z@])\s*")
_PERIOD = re.compile(r"\\bibinitperiod(?![A-Za-z@])\s*")
_NOT_REFERENCE_DATA = ("sort", "label", "extra", "uniq")  # biber's own fields for its sorting and labels


def _plain(value: str) -> str:
    return " ".join(_PERIOD.sub(".", _HYPHEN.sub("-", _NAME_DELIMITER.sub(" ", value))).split())


def _groups(text: str) -> list[str]:
    """The brace groups that follow each other at the start of `text`."""
    groups, pos = [], 0
    while True:
        group = read_group(text, pos, LIMIT)
        if group is None:
            return groups
        groups.append(group[0])
        pos = group[1]


def _person(text: str) -> str:
    """"Family, Given" of one biber name, von and Jr parts kept in BibTeX's order."""
    parts = {}
    for m in _NAME_PART.finditer(text):
        group = read_group(text, m.end(), LIMIT)
        if group:
            parts[m.group(1)] = _plain(group[0])
    family = " ".join(p for p in (parts.get("prefix"), parts.get("family")) if p)
    return ", ".join(p for p in (family, parts.get("suffix"), parts.get("given")) if p)


def _entry_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in _VERB.finditer(body):  # doi, url...: raw text, where a % is no comment
        value = "".join(_VERB_LINE.findall(m.group(2))).strip()
        if value and "{" not in value and "}" not in value:
            fields[m.group(1).lower()] = value
    body = _COMMENT.sub("", _VERB.sub("", body))
    pos = 0
    while True:
        m = _DATA.search(body, pos)
        if m is None:
            return fields
        pos = m.end()
        kind = m.group(1)
        groups = []  # \field{name}{value}, \name{role}{count}{options}{people}, \list{role}{count}{items}
        for _ in range({"field": 2, "name": 4, "list": 3}[kind]):
            group = read_group(body, pos, LIMIT)
            if group is None:
                break
            groups.append(group[0])
            pos = group[1]
        if not groups or len(groups) < {"field": 2, "name": 4, "list": 3}[kind]:
            continue
        name = groups[0].strip().lower()
        if kind == "field":
            if not name.startswith(_NOT_REFERENCE_DATA):
                fields[name] = _plain(groups[1])
        elif kind == "name":
            people = [p for p in (_person(person) for person in _groups(groups[3])) if p]
            if people:
                fields[name] = " and ".join(people)
        else:
            items = [_plain(item) for item in _groups(groups[2])]
            if items:
                fields[name] = " and ".join(items)


def _biblatex_to_bibtex(text: str) -> str:
    """BibTeX entries of every \\entry of a biber .bbl."""
    out, pos = [], 0
    while True:
        m = _ENTRY.search(text, pos)
        if m is None:
            return "".join(out)
        key = read_group(text, m.end(), LIMIT)
        kind = read_group(text, key[1], LIMIT) if key else None
        if kind is None:
            pos = m.end()
            continue
        end = _END_ENTRY.search(text, kind[1])
        next_entry = _ENTRY.search(text, kind[1])
        stop = min(x.start() for x in (end, next_entry) if x) if end or next_entry else len(text)
        pos = stop
        fields = _entry_fields(text[kind[1]:stop])
        lines = "".join(f"  {name} = {{{value}}},\n" for name, value in fields.items())
        out.append(f"@{kind[0].strip()}{{{key[0].strip()},\n{lines}}}\n\n")


def bbl_parts(text: str) -> tuple[str, str]:
    """The thebibliography environments of a .bbl and the BibTeX of its biblatex
    entries. What surrounds them (\\providecommand...) is no text of the paper."""
    biblatex = _biblatex_to_bibtex(text) if _ENTRY.search(text) else ""
    stripped, _ = strip_comments(text)
    found = []
    for m in _THEBIB.finditer(stripped):
        env = m.group(0)
        found.append(env if re.search(r"\\end\s*\{thebibliography\}\Z", env) else env + "\n\\end{thebibliography}")
    if not found and "\\bibitem" in stripped:  # \bibitem without their environment
        at = stripped.index("\\bibitem")
        found.append("\\begin{thebibliography}{}\n" + stripped[at:] + "\n\\end{thebibliography}")
    return "\n".join(found), biblatex
