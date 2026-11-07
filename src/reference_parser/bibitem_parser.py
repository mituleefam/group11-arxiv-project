"""Member 3 - Milestone 2: find the references of one version.

    entries = parse(text, bib_files)

text       the whole document of the version (gatherer.Gathered.text: every
           \\input replaced by its file, comments removed). The references
           are read from this RAW text, before cleaner.py and hierarchy.py
           drop the thebibliography block.
bib_files  the content of the .bib files the document names with
           \\bibliography{...} / \\addbibresource{...} (or, when it names none,
           the folder's .bib files: gatherer.bib_fallback), by relative path.

Three sources are read, in this order:

  1. \\bibitem[label]{key} text   inside thebibliography (or anywhere, when a
     \\bibitem stands outside it). The text stays raw here; bibtex_converter
     turns it into fields. REVTeX/natbib .bbl markup (\\bibinfo{title}{...},
     \\bibfield{author}{...}) is read as fields directly.
  2. amsrefs: \\bib{key}{type}{author={...}, title={...}, ...}.
  3. .bib entries (@article{key, title = {...}, ...}), with @string
     abbreviations, # concatenation and crossref inheritance. Only the
     entries the document cites are kept (\\cite, \\citep, \\nocite, ...),
     as BibTeX itself does; \\nocite{*}, or a document with no \\cite at all,
     keeps every entry. Uncited entries are counted in LAST_STATS.

A key defined twice in one version keeps its first definition (as BibTeX),
except that a \\bibitem and a .bib entry with the same key are joined: the
.bib fields, and the \\bibitem text to fill what they lack.

Each entry is a dict:
    {"citation_key": str, "source": "bibitem" | "bib" | "amsrefs" | "bib+bibitem",
     "entry_type": str | None, "fields": {name: raw LaTeX value}, "raw": str,
     "label": str | None, "origin": "tex" | <.bib path>}

LAST_STATS holds the counts of the last call (read by reference_stats.py).
"""

from __future__ import annotations

import logging
import re

from reference_parser.ref_text import read_braced, read_bracketed, skip_spaces, strip_tex_comments

log = logging.getLogger("m2.references")

LAST_STATS: dict = {}

# ---------------------------------------------------------------------------
# \cite keys of the document
# ---------------------------------------------------------------------------

CITE_COMMAND = re.compile(r"\\([A-Za-z]*[Cc]ite[A-Za-z]*|bibentry)\*?(?![A-Za-z@])")


def iter_cite_groups(text: str):
    """Every key group of every \\cite-like command: yields (start, end) of
    the text between the braces. Handles \\cite[p.~3]{a,b}, \\citep[see][]{a},
    biblatex \\parencite*{a} and multicites \\cites(pre)(post)[x]{a}[y]{b}."""
    for m in CITE_COMMAND.finditer(text):
        name = m.group(1)
        if name.lower() in ("citestyle", "citeindexfalse", "citeindextrue",
                            "citenamefont", "citenumfont", "citeauthoryear"):  # the labels of a .bbl: no keys
            continue
        multi = name.endswith("cites")
        i = m.end()
        while True:
            i = skip_spaces(text, i)
            while i < len(text) and text[i] in "[(":     # optional notes
                if text[i] == "[":
                    group = read_bracketed(text, i)
                else:
                    close = text.find(")", i)
                    group = (None, close + 1) if 0 <= close - i < 200 else None
                if group is None:
                    break
                i = skip_spaces(text, group[1])
            group = read_braced(text, i)
            if group is None:
                break
            yield i + 1, group[1] - 1
            i = group[1]
            if not multi:
                break
            j = skip_spaces(text, i)
            if j >= len(text) or text[j] not in "[{":
                break


def cited_keys(text: str) -> tuple[list[str], bool]:
    """(keys in order of first citation, whether \\nocite{*} is present)."""
    keys, seen, everything = [], set(), False
    for start, end in iter_cite_groups(text):
        for key in text[start:end].split(","):
            key = key.strip()
            if key == "*":
                everything = True
            elif key and key not in seen:
                seen.add(key)
                keys.append(key)
    return keys, everything


# ---------------------------------------------------------------------------
# \bibitem
# ---------------------------------------------------------------------------

BIB_ENVS = ("thebibliography", "references", "bibliography", "biblist", "bibdiv")
_BEGIN_BIB = re.compile(r"\\begin\s*\{(" + "|".join(BIB_ENVS) + r")\*?\}")
_BIBITEM = re.compile(r"\\bibitem(?![A-Za-z@])\s*")
_BIBINFO = re.compile(r"\\bib(?:info|field)\s*\{([^{}]*)\}\s*")
_ITEM_END = re.compile(r"\\end\s*\{(?:" + "|".join(BIB_ENVS) + r")\*?\}")


def _bib_blocks(text: str) -> list[str]:
    """The bodies of the bibliography environments; the whole text when a
    \\bibitem stands outside them (some sources define their own list)."""
    blocks = []
    for m in _BEGIN_BIB.finditer(text):
        end = _ITEM_END.search(text, m.end())
        stop = end.start() if end else len(text)
        body = text[m.end():stop]
        if m.group(1) == "thebibliography":   # its {widest-label} argument
            arg = read_braced(body, skip_spaces(body, 0))
            if arg:
                body = body[arg[1]:]
        blocks.append(body)
    if not blocks or sum(len(_BIBITEM.findall(b)) for b in blocks) < len(_BIBITEM.findall(text)):
        return [text] if _BIBITEM.search(text) else blocks
    return blocks


def bibinfo_fields(raw: str) -> dict[str, str]:
    """REVTeX/natbib markup: \\bibinfo{title}{...}, \\bibfield{author}{...}.
    Several \\bibinfo{author} become one "a and b" value, as in a .bib."""
    fields: dict[str, list[str]] = {}
    for m in _BIBINFO.finditer(raw):
        group = read_braced(raw, m.end())
        if group is None:
            continue
        name = m.group(1).strip().lower()
        if name in ("author", "editor"):
            # \bibfield{author}{\bibinfo{author}{A}, \bibinfo{author}{B}}: the inner ones count
            if _BIBINFO.search(group[0]):
                continue
        fields.setdefault(name, []).append(group[0].strip())
    return {name: (" and ".join(v) if name in ("author", "editor") else v[0]) for name, v in fields.items()}


# Journal macros of the astronomy classes (aastex, mnras...), whose definitions are not in the source
JOURNAL_MACROS = {"apj": "ApJ", "apjl": "ApJL", "apjs": "ApJS", "aj": "AJ", "mnras": "MNRAS", "aap": "A&A",
                  "aaps": "A&AS", "aapr": "A&ARv", "araa": "ARA&A", "pasp": "PASP", "pasj": "PASJ", "pasa": "PASA",
                  "nat": "Nature", "jcap": "JCAP", "prd": "Phys. Rev. D", "prl": "Phys. Rev. Lett.",
                  "pra": "Phys. Rev. A", "prb": "Phys. Rev. B", "prc": "Phys. Rev. C", "pre": "Phys. Rev. E",
                  "physrep": "Phys. Rep.", "apss": "Ap&SS", "na": "New Astron.", "nar": "New Astron. Rev.",
                  "solphys": "Sol. Phys.", "ssr": "Space Sci. Rev.", "memsai": "Mem. Soc. Astron. Italiana",
                  "qjras": "QJRAS", "bain": "Bull. Astron. Inst. Netherlands", "jgr": "J. Geophys. Res."}
# Read by name further on: an author's own definition of these must not replace them
NOT_EXPANDED = {"doi", "url", "href", "path", "nolinkurl", "newblock", "bibinfo", "bibfield", "emph", "textit",
                "textbf", "textsl", "bibitem", "cite", "it", "bf", "em", "sl"}
_DEFINE = re.compile(r"\\(?:re|provide)?(?:new)?command\*?\s*(?:\{\s*\\([A-Za-z@]+)\s*\}|\\([A-Za-z@]+))"
                     r"|\\[gex]?def\s*\\([A-Za-z@]+)")
_DEF_PARAMETERS = re.compile(r"(?:\[#[1-9]\]|#[1-9])*")
_MACRO_USE = re.compile(r"\\([A-Za-z@]+)")
_PROGRAMMING = re.compile(r"\\(?:expandafter|csname|ifx|if[a-z@]*|typeout|let|[gex]?def|newcommand|providecommand|"
                          r"renewcommand|global|begingroup|relax|hskip|catcode)(?![A-Za-z@])")
_IF_NEXT = re.compile(r"\s*\\@ifnextchar\s*\[")
MAX_EXPANDED = 20_000  # characters of one expanded reference: a macro that calls itself stops here


def reference_macros(text: str) -> dict[str, tuple[list[str], str | None, str]]:
    """The macros the document defines: name -> (argument kinds, default of an
    optional first argument, body). A kind is "brace" or "bracket"."""
    # \foreignlanguage{en}{text}: what IEEEtran's .bbl defines with TeX programming, only the text counts
    macros: dict[str, tuple[list[str], str | None, str]] = {"foreignlanguage": (["brace", "brace"], None, "#2")}
    for m in _DEFINE.finditer(text):
        name = m.group(1) or m.group(2) or m.group(3)
        at = m.end()
        kinds: list[str] = []
        default = None
        if m.group(3):                                   # \def\name[#1]#2{body}
            parameters = _DEF_PARAMETERS.match(text, at)
            kinds = ["bracket" if p.startswith("[") else "brace" for p in re.findall(r"\[#[1-9]\]|#[1-9]", parameters.group(0))]
            at = parameters.end()
        else:                                            # \newcommand{\name}[2][default]{body}
            at = skip_spaces(text, at)
            count = read_bracketed(text, at)
            if count and count[0].strip().isdigit():
                kinds = ["brace"] * int(count[0])
                at = skip_spaces(text, count[1])
                optional = read_bracketed(text, at)
                if optional and kinds:
                    default, at, kinds[0] = optional[0], skip_spaces(text, optional[1]), "optional"
        body = read_braced(text, skip_spaces(text, at))
        if body is not None and name not in NOT_EXPANDED and not _PROGRAMMING.search(body[0]):
            macros.setdefault(name, (kinds, default, body[0]))
    return macros


def _macro_arguments(text: str, at: int, kinds: list[str], default: str | None) -> tuple[list[str], int] | None:
    """The arguments that follow a macro, and where they end; None when they are not there."""
    values = []
    for kind in kinds:
        at = skip_spaces(text, at)
        if kind == "brace":
            group = read_braced(text, at)
            if group is None:
                return None
            values.append(group[0])
            at = group[1]
        else:
            group = read_bracketed(text, at)
            if group is None and kind == "bracket":
                return None
            values.append(group[0] if group else default or "")
            at = group[1] if group else at
    return values, at


def expand_macros(raw: str, macros: dict) -> str:
    """A reference with the author's macros replaced, so that a list written as
    \\article{authors}{journal}{volume}{page}{year}{title} reads as plain text."""
    out, pos = [], 0
    while True:
        m = _MACRO_USE.search(raw, pos)
        if m is None or len(raw) > MAX_EXPANDED:
            out.append(raw[pos:])
            return "".join(out)
        name = m.group(1)
        out.append(raw[pos:m.start()])
        pos = m.end()
        if name in macros:
            kinds, default, body = macros[name]
            if not kinds and _IF_NEXT.match(body):       # \@ifnextchar[{\with}{\without}
                first = read_braced(body, _IF_NEXT.match(body).end())
                second = read_braced(body, first[1]) if first else None
                if second:
                    raw = (first[0] if raw[skip_spaces(raw, pos):skip_spaces(raw, pos) + 1] == "[" else second[0]) + raw[pos:]
                    pos = 0
                    continue
            arguments = _macro_arguments(raw, pos, kinds, default)
            if arguments is None:
                out.append(m.group(0))
                continue
            values, end = arguments
            filled = re.sub(r"#([1-9])", lambda x: values[int(x.group(1)) - 1] if int(x.group(1)) <= len(values) else "", body)
            raw, pos = filled + " " + raw[end:], 0
        elif name in JOURNAL_MACROS:
            out.append(JOURNAL_MACROS[name])
            if raw[pos:pos + 2] == "{}":
                pos += 2
        else:
            out.append(m.group(0))


def parse_bibitems(text: str) -> list[dict]:
    """Every \\bibitem[label]{key} text of the document."""
    entries = []
    macros = reference_macros(text)
    for block in _bib_blocks(text):
        starts = list(_BIBITEM.finditer(block))
        for n, m in enumerate(starts):
            i, label = m.end(), None
            opt = read_bracketed(block, i)
            if opt:
                label, i = opt[0].strip(), skip_spaces(block, opt[1])
            key_group = read_braced(block, i)
            if key_group is None:
                log.warning("bibitem without a {key}: %r", block[m.start():m.start() + 60])
                continue
            key = key_group[0].strip()
            stop = starts[n + 1].start() if n + 1 < len(starts) else len(block)
            raw = expand_macros(block[key_group[1]:stop].strip(), macros)
            if not key:
                continue
            entries.append({"citation_key": key, "source": "bibitem", "entry_type": None,
                            "fields": bibinfo_fields(raw), "raw": raw, "label": label, "origin": "tex"})
    return entries


# ---------------------------------------------------------------------------
# amsrefs \bib{key}{type}{fields}
# ---------------------------------------------------------------------------

_AMSBIB = re.compile(r"\\bib\*?\s*(?=\{)")


def parse_amsrefs(text: str) -> list[dict]:
    entries = []
    for m in _AMSBIB.finditer(text):
        key = read_braced(text, m.end())
        kind = read_braced(text, skip_spaces(text, key[1])) if key else None
        body = read_braced(text, skip_spaces(text, kind[1])) if kind else None
        if not body:
            continue
        fields = _fields(body[0], {})
        # amsrefs repeats author={...} once per author
        authors = re.findall(r"\bauthor\s*=\s*\{((?:[^{}]|\{[^{}]*\})*)\}", body[0])
        if len(authors) > 1:
            fields["author"] = " and ".join(a.strip() for a in authors)
        entries.append({"citation_key": key[0].strip(), "source": "amsrefs",
                        "entry_type": kind[0].strip().lower(), "fields": fields, "raw": "",
                        "label": None, "origin": "tex"})
    return entries


# ---------------------------------------------------------------------------
# .bib files
# ---------------------------------------------------------------------------

MONTHS = {m: m for m in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")}
_ENTRY = re.compile(r"@\s*([A-Za-z]+)\s*([{(])")
_FIELD_NAME = re.compile(r"\s*([A-Za-z][\w\-:.+/]*)\s*=\s*")


def _value(s: str, i: int, strings: dict) -> tuple[str, int]:
    """One field value at s[i]: {...}, "...", a number or an @string name,
    joined with #. Returns (value, index after it)."""
    parts = []
    while True:
        i = skip_spaces(s, i)
        if i >= len(s):
            break
        c = s[i]
        if c == "{":
            group = read_braced(s, i)
            if group is None:
                parts.append(s[i + 1:])
                return "".join(parts), len(s)
            parts.append(group[0])
            i = group[1]
        elif c == '"':
            j, depth = i + 1, 0
            while j < len(s):
                if s[j] == "\\":
                    j += 2
                    continue
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                elif s[j] == '"' and depth <= 0:
                    break
                j += 1
            parts.append(s[i + 1:j])
            i = j + 1
        else:
            m = re.compile(r"[^\s,#})]+").match(s, i)
            if not m:
                break
            word = m.group(0)
            parts.append(strings.get(word.lower(), MONTHS.get(word.lower(), word)))
            i = m.end()
        i = skip_spaces(s, i)
        if i < len(s) and s[i] == "#":
            i += 1
            continue
        break
    return "".join(parts), i


def _fields(body: str, strings: dict) -> dict[str, str]:
    """name = value pairs of an entry body (after its key)."""
    fields, i = {}, 0
    while i < len(body):
        m = _FIELD_NAME.match(body, i)
        if not m:
            nxt = body.find(",", i)
            if nxt < 0:
                break
            i = nxt + 1
            continue
        value, i = _value(body, m.end(), strings)
        name = m.group(1).lower()
        if name not in fields:
            fields[name] = " ".join(value.split())
        i = skip_spaces(body, i)
        if i < len(body) and body[i] == ",":
            i += 1
    return fields


def parse_bib(content: str, origin: str = "") -> list[dict]:
    """Every entry of one .bib file. @comment and @preamble are skipped,
    @string abbreviations are expanded; a broken entry is skipped (logged)
    and reading goes on at the next @."""
    strings: dict[str, str] = {}
    entries = []
    content = _strip_bib_comments(content)
    for m in _ENTRY.finditer(content):
        kind = m.group(1).lower()
        open_at = m.end() - 1
        if m.group(2) == "{":
            group = read_braced(content, open_at)
        else:
            close = content.find(")\n", open_at)
            group = (content[open_at + 1:close], close + 1) if close > 0 else None
        if group is None:
            log.warning("%s: entry @%s at %d is not closed, skipped", origin or ".bib", kind, m.start())
            continue
        body = group[0]
        if kind in ("comment", "preamble"):
            continue
        if kind == "string":
            strings.update({k: v for k, v in _fields(body, strings).items()})
            continue
        comma = body.find(",")
        key = (body[:comma] if comma >= 0 else body).strip()
        if not key or "=" in key:
            log.warning("%s: @%s without a key, skipped", origin or ".bib", kind)
            continue
        fields = _fields(body[comma + 1:], strings) if comma >= 0 else {}
        entries.append({"citation_key": key, "source": "bib", "entry_type": kind, "fields": fields,
                        "raw": "", "label": None, "origin": origin})
    return entries


def _strip_bib_comments(content: str) -> str:
    """Lines that start with % are comments in practice (BibTeX ignores text
    outside entries, and biber reads % as a comment)."""
    return re.sub(r"(?m)^[ \t]*%.*$", "", content)


def _crossref(entries: dict[str, dict]) -> None:
    """A field missing from an entry is taken from the entry its crossref names."""
    lower = {k.lower(): e for k, e in entries.items()}
    for e in entries.values():
        parent = lower.get(e["fields"].get("crossref", "").strip().lower())
        if parent is None:
            continue
        for name, value in parent["fields"].items():
            if name == "title" and "booktitle" not in e["fields"]:
                e["fields"]["booktitle"] = value
            e["fields"].setdefault(name, value)


# ---------------------------------------------------------------------------
# One version
# ---------------------------------------------------------------------------

def parse(text: str, bib_files: dict[str, str]) -> list[dict]:
    """The references of one version, in reading order (see the module doc)."""
    text = strip_tex_comments(text or "")
    keys, everything = cited_keys(text)
    cited = {k.lower() for k in keys}

    items = parse_bibitems(text) + parse_amsrefs(text)
    by_key: dict[str, dict] = {}
    duplicates = 0
    for e in items:
        k = e["citation_key"].lower()
        if k in by_key:
            duplicates += 1
            continue
        by_key[k] = e

    bib_entries: dict[str, dict] = {}
    bib_total = 0
    for path, content in (bib_files or {}).items():
        for e in parse_bib(content or "", path):
            bib_total += 1
            k = e["citation_key"].lower()
            if k in bib_entries:
                duplicates += 1
                continue
            bib_entries[k] = e
    _crossref(bib_entries)

    keep_all = everything or not cited
    uncited = 0
    for k, e in bib_entries.items():
        if not keep_all and k not in cited:
            uncited += 1
            continue
        if e["fields"].get("crossref") and k not in cited and not keep_all:
            continue
        if k in by_key:  # \bibitem and .bib entry of the same key: one entry
            item = by_key[k]
            item["fields"] = {**item["fields"], **e["fields"]}
            item["entry_type"] = e["entry_type"]
            item["source"] = "bib+bibitem"
            item["origin"] = e["origin"]
        else:
            by_key[k] = e

    entries = list(by_key.values())
    LAST_STATS.clear()
    LAST_STATS.update(bibitems=sum(e["source"] in ("bibitem", "bib+bibitem") for e in entries),
                      amsrefs=sum(e["source"] == "amsrefs" for e in entries),
                      bib_entries_total=bib_total,
                      bib_entries_used=sum(e["source"] in ("bib", "bib+bibitem") for e in entries),
                      bib_uncited_dropped=uncited, duplicate_keys=duplicates,
                      cited_keys=len(keys), cited_not_found=len(cited - set(by_key)),
                      entries=len(entries))
    if duplicates:
        log.info("%d citation key(s) defined twice in one version: first definition kept", duplicates)
    if keys and not entries:
        log.info("%d key(s) cited but no reference found (a .bbl not kept by Milestone 1?)", len(keys))
    return entries
