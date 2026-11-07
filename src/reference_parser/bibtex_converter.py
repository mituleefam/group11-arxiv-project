"""Member 3 - Milestone 2: turn parsed references into rows of the bibtex table.

    rows = convert(entries)        # entries: bibitem_parser.parse(...)

Each row has the columns of the bibtex table but arxiv_id (the pipeline adds
it):

    citation_key  str, never empty
    entry_type    article, inproceedings, book, incollection, phdthesis,
                  mastersthesis, techreport, misc, ... (misc when unknown)
    title         plain text: no braces, accents as Unicode ({\\"a} -> ä)
    authors       ["Vaswani, Ashish", "Shazeer, Noam"]: "Last, First" each
    year          int (1800 .. next year) or None
    doi           "10.xxxx/..." lower case, no resolver prefix, or None
    journal       journal, or the proceedings / book a paper appears in
    url           a link: url field, arXiv abs page, or doi.org link

and one private column, "_source" ("bib", "bibitem", ...), used by
dedup_bibtex to prefer structured values; the pipeline drops it.

A .bib / amsrefs / \\bibinfo entry already has named fields: they are only
cleaned. A \\bibitem is free text, read with heuristics:

  * DOI, URL and arXiv id by regular expression (before anything else);
  * year: the natbib label "(2017)", then "(2017)" after the authors, then
    the last year of the text outside links and identifiers;
  * the blocks: \\newblock (BibTeX .bbl output: authors / title / venue),
    or a quoted ``title'' (IEEE), or "Authors (2017). Title. Venue" (APA),
    or "Last, F., Last, F. Title. Venue", or "F. Last, F. Last. Title. Venue"
    cut at the sentence ends that are not after an initial or an
    abbreviation (et al., Proc., Vol., ...);
  * entry_type from words of the venue (Proceedings, Conference, Journal,
    Trans., thesis, Technical report, ...), misc when nothing fits.
"""

from __future__ import annotations

import datetime
import logging
import re

from reference_parser.ref_text import (URL_RE, clean_field, strip_end, find_arxiv_id, find_doi, latex_to_text, normalize_doi,
                      read_braced, skip_spaces)

log = logging.getLogger("m2.references")

FIELDS = ["citation_key", "entry_type", "title", "authors", "year", "doi", "journal", "url"]
MAX_YEAR = datetime.date.today().year + 1
LAST_STATS: dict = {}

ENTRY_TYPES = {"article", "book", "booklet", "inbook", "incollection", "inproceedings", "manual",
               "mastersthesis", "misc", "phdthesis", "proceedings", "techreport", "unpublished"}
TYPE_ALIASES = {"conference": "inproceedings", "online": "misc", "electronic": "misc", "www": "misc",
                "webpage": "misc", "software": "misc", "dataset": "misc", "report": "techreport",
                "thesis": "phdthesis", "collection": "book", "mvbook": "book", "reference": "book",
                "inreference": "incollection", "suppbook": "inbook", "patent": "misc",
                "periodical": "article", "preprint": "misc"}

# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------

_YEAR = re.compile(r"(?<![\d.\-/])(1[89]\d\d|20\d\d)(?![\d])")


def to_year(value) -> int | None:
    if value is None:
        return None
    m = _YEAR.search(str(value))
    if not m:
        return None
    year = int(m.group(1))
    return year if 1800 <= year <= MAX_YEAR else None


def normalize_type(kind: str | None, fields: dict | None = None) -> str:
    kind = (kind or "").strip().lower()
    kind = TYPE_ALIASES.get(kind, kind)
    if kind == "phdthesis" and fields and re.search(r"master|m\.?\s?sc|msc", fields.get("type", ""), re.I):
        kind = "mastersthesis"
    return kind if kind in ENTRY_TYPES else "misc"


# ---- names ----

PARTICLES = {"van", "von", "der", "den", "de", "di", "da", "del", "della", "des", "du", "dos", "das",
             "la", "le", "ten", "ter", "op", "zu", "zur", "bin", "al", "el", "y"}
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
_AND = re.compile(r"\s+and\s+", re.I)


def _tokens_top(s: str) -> list[str]:
    """Words of a name, a {braced group} being one word."""
    words, i, cur = [], 0, ""
    while i < len(s):
        c = s[i]
        if c == "{":
            group = read_braced(s, i)
            if group:
                cur += s[i:group[1]]
                i = group[1]
                continue
        if c in " ~\t\n":
            if cur:
                words.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    if cur:
        words.append(cur)
    return words


def _split_and(s: str) -> list[str]:
    """Split a BibTeX author list at " and " outside braces."""
    out, depth, start, i = [], 0, 0, 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth == 0:
            m = _AND.match(s, i)
            if m:
                out.append(s[start:i])
                start = i = m.end()
                continue
        i += 1
    out.append(s[start:])
    return [p.strip() for p in out if p.strip()]


def _join(last: str, first: str) -> str:
    last, first = latex_to_text(last).strip(" ,."), latex_to_text(first).strip(" ,")
    if re.fullmatch(r"[A-Z]{1,3}", first):            # "Smith J" / "Doe AB": bare initials get their dots
        first = " ".join(c + "." for c in first)
    elif re.fullmatch(r"(?:[A-Z]\.\s*)*[A-Z]", first):   # "N" left without its dot at the end
        first += "."
    if not last:
        return first
    return f"{last}, {first}" if first else last


def bib_name(name: str) -> str | None:
    """One BibTeX name ("Last, First", "Last, Jr, First" or "First von Last")
    as "Last, First". None for "others"."""
    name = name.strip()
    if not name or name.lower() in ("others", "et al.", "et al"):
        return None
    if name.startswith("{") and read_braced(name, 0) and read_braced(name, 0)[1] == len(name):
        return latex_to_text(name)                     # {Google Research}: one name, kept whole
    parts = _split_commas(name)
    if len(parts) == 2:
        return _join(parts[0], parts[1])
    if len(parts) >= 3:
        return _join(f"{parts[0]} {parts[1]}", parts[2])
    words = _tokens_top(name)
    if len(words) == 1:
        return latex_to_text(words[0])
    # First von Last: the last name starts at the first lower-case word after the first one
    start = len(words) - 1
    for k in range(1, len(words) - 1):
        if words[k][:1].islower() and words[k][:1].isalpha():
            start = k
            break
    if words[-1].lower().strip(".") in {s.strip(".") for s in SUFFIXES} and len(words) > 2:
        return _join(" ".join(words[start - 1 if start == len(words) - 1 else start:]),
                     " ".join(words[:start - 1 if start == len(words) - 1 else start]))
    return _join(" ".join(words[start:]), " ".join(words[:start]))


def _split_commas(s: str) -> list[str]:
    out, depth, start = [], 0, 0
    for i, c in enumerate(s):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(s[start:i])
            start = i + 1
    out.append(s[start:])
    return [p.strip() for p in out]


def bib_authors(value: str | None) -> list[str]:
    if not value:
        return []
    return [n for n in (bib_name(p) for p in _split_and(value)) if n]


# ---- free-text author lists (\bibitem) ----

_INITIALS = r"(?:[A-Z\u00C0-\u024F][a-z]?(?:\.|(?=\s*(?:,|$|&|\band\b)))(?:\s*-\s*|\s*)?)+"
_LAST_FIRST = re.compile(r"\s*(?:(?:and|&)\s+)?(?P<last>(?:[a-z]+\s+)*[A-Z\u00C0-\u024F][\w'\u00C0-\u024F\-]*"
                         r"(?:\s+[A-Z\u00C0-\u024F][\w'\u00C0-\u024F\-]*){0,2}),\s*"
                         r"(?P<first>" + _INITIALS + r")")
_ETAL = re.compile(r",?\s*(?:et\.?\s*al\.?|and\s+others)\s*", re.I)
_NAME_SEP = re.compile(r"\s*(?:,\s*and\s+|,\s*&\s*|\s+and\s+|\s*&\s*|;\s*|,\s*)", re.I)
_EDS = re.compile(r"\(\s*eds?\.?\s*\)|,?\s*\beds?\.\s*$|,?\s*editors?\s*$", re.I)


def person_name(s: str) -> str | None:
    """"A. Vaswani", "Ashish Vaswani", "Vaswani A" -> "Vaswani, A." style."""
    s = s.strip(" .,;:")
    if not s or len(s) > 80 or re.search(r"\d", s):
        return None
    if "," in s:
        last, first = s.split(",", 1)
        return _join(last, first)
    words = s.split()
    if len(words) == 1:
        return s
    # "Vaswani A" / "Vaswani AB": a family name then bare initials
    if re.fullmatch(r"[A-Z]{1,3}", words[-1]) and not re.fullmatch(r"[A-Z]\.?", words[0]):
        return _join(" ".join(words[:-1]), words[-1])
    start = len(words) - 1
    for k in range(1, len(words) - 1):
        if words[k].lower() in PARTICLES:
            start = k
            break
    if words[-1].lower() in SUFFIXES and len(words) > 2:
        return _join(" ".join(words[start - 1:]), " ".join(words[:start - 1]))
    return _join(" ".join(words[start:]), " ".join(words[:start]))


# "Balinskii A. A.": the family name, then its initials without a comma
_CAPITALS = "A-Z\u00C0-\u024F"
_LAST_THEN_INITIALS = (rf"[{_CAPITALS}][\w'\u2019\-]+(?:[ \-][{_CAPITALS}][\w'\u2019\-]+)?\s+"
                       rf"(?:[{_CAPITALS}]\.[ \-]?)+")
_LAST_THEN_INITIALS_GROUPS = re.compile(rf"([{_CAPITALS}][\w'\u2019\-]+(?:[ \-][{_CAPITALS}][\w'\u2019\-]+)?)\s+"
                                        rf"((?:[{_CAPITALS}]\.?[ \-]?)+)")
_INITIALS_ONLY = re.compile(r"(?:[A-ZÀ-ɏ]\.-?)*[A-ZÀ-ɏ]\.?")  # the last "." of a list was stripped


def _name_initial_pairs(s: str) -> list[str]:
    """"Bai, J., d'Avila Garcez, A., Yang, Yang, H.": a comma stands inside a name
    too, so a piece holding only initials belongs to the piece before it."""
    pieces = [p.strip() for p in s.split(",") if p.strip()]
    if not any(_INITIALS_ONLY.fullmatch(p) for p in pieces):
        return []
    names, k = [], 0
    while k < len(pieces):
        if k + 1 < len(pieces) and _INITIALS_ONLY.fullmatch(pieces[k + 1]):
            names.append(_join(pieces[k], pieces[k + 1]))
            k += 2
        else:
            names.append(pieces[k])
            k += 1
    return names


def text_authors(s: str) -> list[str]:
    """Authors of a free-text author list, as "Last, First"."""
    s = _EDS.sub("", latex_to_text(s)).strip(" .,;:")
    s = re.sub(r",\s*(Jr|Sr|II|III|IV)\.?(?=\s*,)", r" \1", s)  # "Anderson, Jr., J. E."
    s = _ETAL.split(s)[0].strip(" .,;:")
    if not s:
        return []
    # "Vaswani, A., Shazeer, N., and Parmar, N."
    pairs, pos = [], 0
    while True:
        m = _LAST_FIRST.match(s, pos)
        if not m:
            break
        pairs.append(_join(m.group("last"), m.group("first")))
        pos = m.end()
        nxt = re.match(r"\s*(?:,\s*(?:and\s+|&\s*)?|and\s+|&\s*)", s[pos:])
        if not nxt or not nxt.group(0):
            break
        pos += nxt.end()
    if pairs and pos >= len(s.rstrip(" .")) - 1:
        return pairs
    # "Vaswani, Ashish and Shazeer, Noam": BibTeX style written out
    if re.search(r"\s+and\s+", s) and all(p.count(",") == 1 for p in _AND.split(s)):
        return [_join(*p.split(",", 1)) for p in _AND.split(s)]
    pieces = [p for p in re.split(r"\s*,\s*(?:and\s+|&\s*)?|\s+and\s+|\s*&\s*", s) if p]
    last_first = [_LAST_THEN_INITIALS_GROUPS.fullmatch(p) for p in pieces]
    if pieces and all(last_first) and any("." in p for p in pieces):
        return [_join(m.group(1), m.group(2).strip()) for m in last_first]
    return _name_initial_pairs(s) or [n for n in (person_name(p) for p in _NAME_SEP.split(s)) if n]


# ---------------------------------------------------------------------------
# \bibitem text -> fields
# ---------------------------------------------------------------------------

_ABBREV = {"al", "proc", "conf", "int", "intl", "vol", "no", "nos", "pp", "eds", "ed", "jr", "sr", "st",
           "phys", "rev", "lett", "j", "trans", "univ", "dept", "inc", "ltd", "comput", "math", "sci",
           "natl", "acad", "assoc", "mach", "learn", "res", "syst", "inf", "process", "appl", "eng",
           "soc", "stat", "ann", "am", "chem", "biol", "med", "nucl", "astrophys", "astron", "mon",
           "not", "roy", "vs", "cf", "e.g", "i.e", "fig", "eq", "sec", "ch", "chap", "repr", "tech",
           "rep", "symp", "workshop", "adv", "neural", "inform", "comm", "commun", "ieee", "acm",
           "mr", "mrs", "ms", "dr", "prof", "jpn", "eur", "europ", "nat", "theor", "exp", "rep",
           "opt", "electron", "mater", "geophys", "atmos", "oceanogr", "environ", "rec", "ser"}
_MARK_ITALIC = re.compile(r"\\(?:emph|textit|textsl|mathit)\s*(?=\{)|\{\\(?:em|it|sl|itshape)(?![A-Za-z])")
_QUOTED = re.compile(r"``(.+?)''|`(.{8,}?)'(?=[\s,.\[\x06]|$)|\u201c(.+?)\u201d|(?<![\w\\])\"(.{8,}?)\"(?!\w)", re.S)
_NEWBLOCK = re.compile(r"\\newblock(?![A-Za-z])")
_APA_YEAR = re.compile(r"\(\s*((?:1[89]|20)\d\d)[a-z]?(?:,\s*[A-Za-z]+\.?(?:\s+\d{1,2})?)?\s*\)")
_URL_CMD = re.compile(r"\\(?:url|path|nolinkurl)\s*(?=\{)")
_HREF = re.compile(r"\\href\s*(?=\{)")
_DOI_CMD = re.compile(r"\\doi\s*(?=\{)")
_IDS = re.compile(r"(?:doi\s*:?\s*)?10\.\d{4,9}/\S+|https?://\S+|arxiv\s*:?\s*[\w\-./]+(?:\s*\[[^\]]*\])?|"
                  r"\bISBN[\s:\-]*[\dX\-]+|\bISSN[\s:\-]*[\dX\-]+", re.I)
_VENUE_CUT = re.compile(r",\s*(?:pp\.?|pages?|vol\.?|volume|no\.?|number|issue|ed\.|eds\.|editors?|"
                        r"chapter|ch\.|\d)|\s\(?(?:1[89]|20)\d\d\)?\s*[.,;:]?\s*$|\s+\d+\s*\(\d+\)|"
                        r"\s+\d+\s*:\s*\d+|[.;]\s*(?:1[89]|20)\d\d\b", re.I)


def _cut_braced(raw: str, pattern: re.Pattern, keep: str = "") -> tuple[str, list[str]]:
    """Remove every `pattern`{...} group; returns the text and the groups'
    contents. keep="arg" leaves the argument in the text."""
    found, out, pos = [], [], 0
    for m in pattern.finditer(raw):
        if m.start() < pos:
            continue
        group = read_braced(raw, m.end())
        if not group:
            continue
        found.append(group[0])
        out.append(raw[pos:m.start()])
        if keep == "arg":
            out.append(group[0])
        pos = group[1]
    out.append(raw[pos:])
    return "".join(out), found


def _hrefs(raw: str) -> tuple[str, list[str]]:
    """\\href{url}{text} -> text; returns the URLs."""
    urls, out, pos = [], [], 0
    for m in _HREF.finditer(raw):
        if m.start() < pos:
            continue
        first = read_braced(raw, m.end())
        second = read_braced(raw, skip_spaces(raw, first[1])) if first else None
        if not second:
            continue
        urls.append(first[0])
        out.append(raw[pos:m.start()])
        out.append(second[0])
        pos = second[1]
    out.append(raw[pos:])
    return "".join(out), urls


def _mark_italic(raw: str) -> str:
    """\\emph{x}, \\textit{x}, {\\em x} -> \\x05x\\x06, so the venue (or book title)
    set in italics can be found after latex_to_text."""
    out, pos = [], 0
    for m in _MARK_ITALIC.finditer(raw):
        if m.start() < pos:
            continue
        if m.group(0).startswith("{"):
            group = read_braced(raw, m.start())
            if not group:
                continue
            inner = group[0][len(m.group(0)) - 1:]
        else:
            group = read_braced(raw, m.end())
            if not group:
                continue
            inner = group[0]
        out.append(raw[pos:m.start()])
        out.append("\x05" + inner + "\x06")
        pos = group[1]
    out.append(raw[pos:])
    return "".join(out)


def _sentence_ends(s: str) -> list[int]:
    """Positions just after the periods that end a part of a reference: a
    period followed by a space and a capital, a digit, a quote or a marker,
    that does not end an initial (A.) or a usual abbreviation (Proc.)."""
    ends = []
    for m in re.finditer(r"[.?!](?=\s+(?:[\"'\u201c\x05A-Z0-9\u00C0-\u024F(\[]|arXiv|arxiv|in\s))|[.?!](?=\x05)", s):
        word = re.search(r"([\w.\-]+)$", s[:m.start()])
        token = word.group(1) if word else ""
        bare = token.lower().split(".")[-1] if token else ""
        if re.fullmatch(r"(?:[A-Z\u00C0-\u024F][a-z]?\.?-?)+", token) and len(token.replace(".", "").replace("-", "")) <= 3 \
                and token[:1].isupper() and not token.isdigit():
            continue                                     # an initial: "A." or "J.-P."
        if bare in _ABBREV or token.lower() in _ABBREV:
            continue
        ends.append(m.end())
    return ends


# Vancouver / PubMed: "Smith J, Doe AB. Title. J Chem Phys. 2010;12:1-10."
_VANCOUVER = re.compile(r"(?:[A-ZÀ-ɏ][\w'\-]+(?:\s+[A-ZÀ-ɏ][\w'\-]+)?\s+[A-Z]{1,3},\s*)*"
                        r"[A-ZÀ-ɏ][\w'\-]+(?:\s+[A-ZÀ-ɏ][\w'\-]+)?\s+[A-Z]{1,3}"
                        r"(?:,?\s*et\s+al)?\.\s+")

# Springer (LNCS): "Last, F., Last, F.M.: Title. Venue". Only the colon after an
# initial or "et al." ends the authors: "Smith, J. Title: a subtitle" stays as it was.
_SPRINGER = re.compile(r"^\s*[^\s,:][^,:]*,\s*[A-ZÀ-ɏ][^:]*?"
                       r"(?:[A-ZÀ-ɏ]\.|\bet\.?\s+al\.?)(?:\s*\(eds?\.\))?:\s")
_EDITORS = re.compile(r"^[^()]{0,600}\(eds?\.\)\s*")  # the editors before a book's name

_CAPITAL = "A-Z\u00C0-\u024F"
# Mathematics and physics: "F. Bracci, H. Gaussier and A. Zimmer, Title, Journal 126 (2024)".
_INITIAL_NAME = (rf"(?:[{_CAPITAL}]\.[ \-]?)+(?:(?:de|del|der|den|di|du|da|la|le|van|von|ter|el|al)\s+)*"
                 rf"[{_CAPITAL}][\w'\u2019\-]+(?:[ \-][{_CAPITAL}][\w'\u2019\-]+){{0,2}}")
_ANY_NAME = rf"(?:{_INITIAL_NAME}|{_LAST_THEN_INITIALS})"
_INITIAL_AUTHORS = re.compile(rf"\s*{_ANY_NAME}(?:\s*(?:,\s*(?:and\s+|&\s*)?|\s+and\s+|&\s*){_ANY_NAME})*"
                              r"(?:,?\s*et\.?\s*al\.?)?(?=\s*(?:[,.:;]|$))")
# The volume and year that follow a journal's name: "126 (2024)", "139:11 (2011)", "17, 891 (1905)", "9(8)"
_VOLUME = re.compile(r"\b\d+(?::\d+)?(?:\s*\(\d{1,3}\))?(?:\s*,?\s*[Nn]o\.\s*\d+)?(?:\s*[,:]\s*[\w.\-]+)?\s*\((?:1[89]|20)\d\d\)|\b\d+\s*\(\d{1,3}\)")
_JOURNAL_WORD = rf"(?:[{_CAPITAL}][\w.'&\u2019\-]*|of|the|and|in|on|de|for|&)"
_JOURNAL = re.compile(rf"\s*\x05?\s*{_JOURNAL_WORD}(?:\s+{_JOURNAL_WORD}){{0,9}}\s*\x06?[\s,]*")
# What starts the venue when there is no volume to go by
_VENUE_START = re.compile(rf"[.,]\s+(?=\x05?\s*(?:[{_CAPITAL}][a-z]{{0,7}}\.\s|[Ii]n:?\s|work in progress\b|in preparation\b|to appear\b|submitted\b|Conference\b|Proceedings\b|Journal\b|"
                          r"Annals\b|Transactions\b|[Pp]reprint\b|arXiv\b|Ph\.?\s?D\b|Thesis\b|Master|Lecture Notes\b|"
                          r"Springer\b|Cambridge\b|Oxford\b|Wiley\b|Birkh|Academic\b|Elsevier\b|Princeton\b|Kluwer\b|"
                          r"American Math|World Scientific\b|North-Holland\b|CRC\b|Dover\b|MIT Press\b))")
# Astronomy: "White, S. D. M., and Rees, M. J. 1978, MNRAS, 183, 341": the year follows the authors
_YEAR_AFTER_AUTHORS = re.compile(r"\s+((?:1[89]|20)\d\d)[a-z]?[,.:]\s+")
_JOURNAL_ONLY = re.compile(rf"\s*\x05?\s*(?:arXiv\b[\w\s\-]*|{_JOURNAL_WORD}(?:\s+{_JOURNAL_WORD}){{0,7}})\s*\x06?"
                           r"\s*(?:,?\s*[A-Z]?\d|,?\s*(?:in press|in prep|submitted|accepted|arXiv)|[\s,.;]*$)")


def _italic_title(text: str) -> re.Match | None:
    """The italic part when it is the title: right after the authors, several
    words, and not a journal followed by its volume ("A. Author, {\\it Ann. Math.} 12 (2000)")."""
    m = re.search(r"\x05\s*(.+?)\s*\x06", text)
    if m is None or len(m.group(1).split()) < 2:
        return None
    authors = text[:m.start()].strip(" ,.:;")
    pieces = [p for p in re.split(r"\s*,\s*(?:and\s+|&\s*)?|\s+and\s+|\s*&\s*", authors) if p]
    if not pieces or not all(_looks_like_names(p) for p in pieces):
        return None
    after = text[m.end():].lstrip(" ,")
    is_journal = "." in m.group(1) and _JOURNAL.fullmatch(m.group(1)) and _VOLUME.match(after)
    return None if is_journal or _VOLUME.match(after) else m


def _title_and_venue(rest: str) -> tuple[str, str]:
    """A reference without its authors, cut into title and venue: the venue is
    what names the journal before its volume, else what looks like a venue."""
    volume = _VOLUME.search(rest)
    if volume:
        starts = [0] + [m.end() for m in re.finditer(r"[.,]\s+", rest[:volume.start()])]
        for start in starts:
            if _JOURNAL.fullmatch(rest[start:volume.start()]):
                title = rest[:start].strip(" ,.;:")
                ends = _sentence_ends(title + ". X")[:-1]  # "Title. Book of the proceedings, Lecture Notes 12 (2000)"
                return (title[:ends[0]], rest[ends[0]:]) if ends else (title, rest[start:])
    venue = _VENUE_START.search(rest)
    if venue:
        return rest[:venue.start()], rest[venue.end():]
    ends = _sentence_ends(rest)
    if ends:
        return rest[:ends[0]], rest[ends[0]:]
    pieces = rest.split(", ", 1)                         # "Title, Journal 3, 45 (2001)"
    return (pieces[0], pieces[1]) if len(pieces) == 2 else (rest, "")


def _year_after_authors(text: str) -> re.Match | None:
    """The year when it comes right after "Last, F., Last, F." with no title before it."""
    m = _YEAR_AFTER_AUTHORS.search(text)
    if m is None:
        return None
    authors = text[:m.start()]
    return m if authors.endswith(".") and not re.search(r"\d", authors) and _LAST_FIRST.match(authors) else None


def _looks_like_names(piece: str) -> bool:
    words = piece.replace(" and ", " ").split()
    return 0 < len(words) <= 6 and person_name(piece.replace(" and ", " ")) is not None and \
        all(w[:1].isupper() or w.lower() in PARTICLES or w == "&" for w in words) and "\x05" not in piece


def _authors_end(s: str) -> int | None:
    """Where the author list of a reference ends, for "Last, F., Last, F. Title"
    and "... et al. Title"."""
    m = _VANCOUVER.match(s)
    if m:
        return m.end()
    m = re.search(r"\bet\.?\s*al\.?,?\s*", s)
    if m and m.start() < 300:
        rest = s[m.end():]
        if rest[:1].isupper() or rest[:1] in "\x05\"(":
            return m.end()
    pos, count = 0, 0
    while True:
        m = _LAST_FIRST.match(s, pos)
        if not m:
            break
        count += 1
        pos = m.end()
        nxt = re.match(r"\s*(?:,\s*(?:and\s+|&\s*)?|and\s+|&\s*)", s[pos:])
        if not nxt or not nxt.group(0):
            break
        if not _LAST_FIRST.match(s, pos + nxt.end()):
            break
        pos += nxt.end()
    if count:
        return pos
    return None


def _strip_year(s: str) -> str:
    s = _APA_YEAR.sub("", s)
    s = re.sub(r"[,.]?\s*\b(?:1[89]|20)\d\d[a-z]?\s*[.,]?\s*$", "", s)
    return s.strip(" ,.;:")


def _clean_venue(s: str) -> str | None:
    s = s.replace("\x05", "").replace("\x06", "")
    s = re.sub(r"^\s*(?:in|In|IN)\s*:?\s+", "", s.strip(" ,.;:"))
    s = _EDITORS.sub("", s)
    s = _IDS.sub("", s)
    m = _VENUE_CUT.search(s)
    if m and m.start() > 3:
        s = s[:m.start()]
    s = _strip_year(s)
    s = re.sub(r"\s*\((?:\s*)\)\s*", " ", s)
    s = re.sub(r"(?:[\s,]+\d+)+[\s,]*$", "", s)        # a volume number left at the end
    return strip_end(re.sub(r"\s+", " ", s)) or None


def _classify(venue: str, text: str, has_title: bool) -> str:
    low_v = (venue or "").lower()
    low = text.lower()
    if re.search(r"ph\.?\s?d\.?\s*(?:thesis|dissertation)|doctoral\s+(?:thesis|dissertation)|dissertation", low):
        return "phdthesis"
    if re.search(r"master'?s?\s+thesis|m\.?\s?sc\.?\s+thesis|diploma\s+thesis", low):
        return "mastersthesis"
    if re.search(r"technical\s+report|tech\.?\s*rep|\breport\s+no\b|white\s*paper", low):
        return "techreport"
    if re.search(r"\bproc\.|proceedings|conference|conf\.|workshop|symposium|\bsymp\.|"
                 r"advances\s+in\s+neural|\b(?:neurips|nips|icml|iclr|cvpr|iccv|eccv|acl|emnlp|naacl|coling|aaai|ijcai|kdd|"
                 r"sigir|sigmod|vldb|icde|www|interspeech|icassp|miccai|uai|aistats|colt|icra|iros|"
                 r"wsdm|cikm|recsys|chi|uist|osdi|sosp|nsdi|isca|focs|stoc|soda)\b", low_v) \
            or re.match(r"\s*in\b", (venue or "").lower()):
        return "inproceedings"
    if (re.search(r"\b\d+\s*,\s*\d+", low_v) and "arxiv" not in low_v) or re.search(r"journal|\bj\.|trans\.|transactions|letters|\blett\.|review|\brev\.|annals|\bann\.|"
                 r"magazine|quarterly|bulletin|\bvol\.|volume|nature|science\b|\bplos\b|"
                 r"physical review|\bphys\.", low_v) or re.search(r"\d+\s*\(\d+\)|\b\d+\s*:\s*\d+", low_v):
        return "article"
    if re.search(r"arxiv|preprint|corr\b|biorxiv|medrxiv|ssrn", low):
        return "misc"
    if re.search(r"\bpress\b|publisher|springer|wiley|elsevier|addison|o'reilly|prentice|mcgraw|"
                 r"cambridge university|oxford university|\bedition\b|\bed\.\s*$|\bisbn\b", low):
        return "book" if not venue or not has_title else "incollection" if re.search(r"\bin\b", low_v) else "book"
    return "misc"


def parse_free_text(raw: str, label: str | None = None) -> dict:
    """Fields of the free text of one \\bibitem (see the module doc)."""
    out: dict = {}
    # 1. identifiers and links, read on the raw text
    raw, urls = _hrefs(raw)
    raw, more = _cut_braced(raw, _URL_CMD)
    urls += more
    raw, dois = _cut_braced(raw, _DOI_CMD)
    urls += URL_RE.findall(latex_to_text(raw))
    doi = next((normalize_doi(d) for d in dois if normalize_doi(d)), None)
    doi = doi or next((find_doi(u) for u in urls if find_doi(u)), None) or find_doi(latex_to_text(raw))
    arxiv = find_arxiv_id(" ".join(urls)) or find_arxiv_id(latex_to_text(raw))
    page = next((u.rstrip(".,;") for u in urls if "doi.org" not in u.lower()), None)
    out["doi"] = doi
    out["url"] = page or (f"https://arxiv.org/abs/{arxiv}" if arxiv else None) or (
        f"https://doi.org/{doi}" if doi else None)

    # 2. text with markers: \x00 block end (\newblock), \x05 ... \x06 italics
    raw = _NEWBLOCK.sub("\x00", raw)
    raw = _mark_italic(raw)
    text = latex_to_text(raw).replace("\x00", " \x00 ")
    text = re.sub(r"\x05\s*(et\.?\s*al\.?,?)\s*\x06", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    plain = _IDS.sub(" ", text.replace("\x05", "").replace("\x06", "").replace("\x00", ""))

    # 3. year
    year = None
    if label:
        m = re.search(r"\((?:1[89]|20)\d\d[a-z]?\)", latex_to_text(label))
        year = to_year(m.group(0)) if m else None
    if year is None:
        m = _APA_YEAR.search(plain)
        year = to_year(m.group(1)) if m and m.start() < len(plain) * 0.6 else None
    if year is None:
        years = [to_year(y) for y in _YEAR.findall(plain)]
        years = [y for y in years if y]
        year = years[-1] if years else None
    out["year"] = year

    # 4. blocks: authors / title / venue
    authors_s = title = venue = ""
    blocks = [b.strip(" .,") for b in text.split("\x00") if b.strip(" .,")]
    quoted = _QUOTED.search(text)
    apa = _APA_YEAR.search(text)
    extra_venue = ""
    if len(blocks) == 2 and re.match(r"\s*(?:in\b|\x05)", blocks[1], re.I) and _sentence_ends(blocks[0] + " X"):
        # "Authors. Title. \newblock In Venue": authors and title share the first block
        text, extra_venue = blocks[0] + ".", blocks[1]
        blocks = [text]
        quoted, apa = _QUOTED.search(text), _APA_YEAR.search(text)
    if len(blocks) >= 2:
        authors_s, title, venue = blocks[0], blocks[1], " ".join(blocks[2:])
    elif quoted and quoted.start() < len(text) * 0.7:
        authors_s = text[:quoted.start()]
        title = next(g for g in quoted.groups() if g)
        venue = text[quoted.end():]
    elif apa and apa.start() < len(text) * 0.5 and apa.start() > 0 \
            and not re.search(r"\d[\s:,\d]*$", text[:apa.start()]):  # "Journal 61 (2006)" is a volume and its year
        authors_s = text[:apa.start()]
        rest = text[apa.end():].lstrip(" .,:")
        ends = _sentence_ends(rest)
        title, venue = (rest[:ends[0]], rest[ends[0]:]) if ends else (rest, "")
    elif _year_after_authors(text):
        m = _year_after_authors(text)
        authors_s, rest, out["year"] = text[:m.start()], text[m.end():], to_year(m.group(1))
        title, venue = _title_and_venue(rest)
        # "Science China Physics, Mechanics, and Astronomy, 68, 1": a journal whose name has commas
        if _JOURNAL_ONLY.match(rest) or (len(title.split()) <= 4 and _JOURNAL.fullmatch(title)):
            title, venue = "", rest
    elif _italic_title(text):
        m = _italic_title(text)
        authors_s, title, venue = text[:m.start()], m.group(1), text[m.end():]
    elif not _SPRINGER.match(text) and _INITIAL_AUTHORS.match(text):
        m = _INITIAL_AUTHORS.match(text)
        authors_s = m.group(0)
        title, venue = _title_and_venue(text[m.end():].lstrip(" ,.;:"))
    else:
        springer = _SPRINGER.match(text)
        cut = springer.end() if springer else _authors_end(text)
        if cut is None:
            ends = _sentence_ends(text)
            cut = ends[0] if ends else None
        if cut is None:
            # no sentence end: "A. Einstein, Ann. Phys. 17, 891 (1905)" (physics style, no title)
            # or "S. Hochreiter and J. Schmidhuber, Long short-term memory, Neural Computation 9(8)"
            pieces = re.split(r",\s*", text)
            k = 0
            while k < len(pieces) - 1 and _looks_like_names(pieces[k]):
                k += 1
            authors_s, rest = ", ".join(pieces[:k]), pieces[k:]
            if len(rest) >= 2 and len(rest[0].split()) >= 3 and not re.search(r"\d|\x05", rest[0]) \
                    and not _looks_like_names(rest[0]):
                title, venue = rest[0], ", ".join(rest[1:])
            else:
                venue = ", ".join(rest)
        else:
            authors_s, rest = text[:cut].rstrip(" :"), text[cut:].lstrip(" .,:")
            italic = re.match(r"\s*\x05(.+?)\x06", rest)
            if italic:                                 # Title in italics right after the authors (books)
                title, venue = italic.group(1), rest[italic.end():]
            else:
                ends = _sentence_ends(rest)
                if ends:
                    title, venue = rest[:ends[0]], rest[ends[0]:]
                else:
                    pieces = rest.split(", ", 1)        # "Title, Journal 3, 45 (2001)"
                    title, venue = (pieces[0], pieces[1]) if len(pieces) == 2 else (rest, "")
    if extra_venue:
        if title and not venue.strip(" .,"):
            venue = extra_venue
        elif not title:
            title, venue = venue, extra_venue
        else:
            venue = venue + " " + extra_venue
    if re.match(r"\s*in\s", title or "", re.I) and not venue.strip(" .,"):
        title, venue = "", title                       # what was taken for the title is the venue
    # an italic part of the venue is the journal / book series
    italic = re.search(r"\x05(.+?)\x06", venue)
    journal = _clean_venue(italic.group(1)) if italic and len(italic.group(1)) > 2 else _clean_venue(venue)

    authors = text_authors(_strip_year(authors_s.replace("\x05", "").replace("\x06", "")))
    title = clean_field(_strip_year(title.replace("\x05", "").replace("\x06", "")) if title else None)
    if title and (len(title) < 3 or not re.search(r"[A-Za-z\u00C0-\u024F]", title)):
        title = None
    out.update(authors=authors, title=title, journal=journal)
    out["entry_type"] = _classify(venue.replace("\x05", "").replace("\x06", ""),
                                  text.replace("\x05", "").replace("\x06", ""), bool(title))
    if out["entry_type"] == "misc" and arxiv and not journal:
        out["journal"] = f"arXiv preprint arXiv:{arxiv}"
    return out


# ---------------------------------------------------------------------------
# Named fields (.bib, amsrefs, \bibinfo) -> row values
# ---------------------------------------------------------------------------

def from_fields(f: dict, entry_type: str | None) -> dict:
    """Row values of an entry with named fields."""
    title = clean_field(f.get("title") or (f.get("booktitle") if entry_type in ("proceedings", "book") else None))
    authors = bib_authors(f.get("author")) or bib_authors(f.get("editor"))
    year = to_year(f.get("year")) or to_year(f.get("date"))
    doi = normalize_doi(f.get("doi")) or find_doi(f.get("url", "")) or find_doi(f.get("note", ""))
    eprint = f.get("eprint", "")
    archive = (f.get("archiveprefix") or f.get("eprinttype") or "").lower()
    arxiv = find_arxiv_id(eprint if "arxiv" in archive else "") or (
        eprint.strip().lower() if "arxiv" in archive and eprint.strip() else None) or \
        find_arxiv_id(f.get("journal", "")) or find_arxiv_id(f.get("url", "")) or find_arxiv_id(f.get("note", ""))
    url = latex_to_text(f.get("url")) or None
    if not url:
        how = f.get("howpublished", "") + " " + f.get("note", "")
        found = re.search(r"\\url\s*\{([^{}]+)\}|https?://[^\s{}]+", how)
        url = (found.group(1) or found.group(0)) if found else None
    url = url or (f"https://arxiv.org/abs/{arxiv}" if arxiv else None) or (f"https://doi.org/{doi}" if doi else None)
    journal = clean_field(f.get("journal") or f.get("journaltitle") or
                          (f.get("booktitle") if entry_type not in ("proceedings", "book") else None) or
                          f.get("series"))
    if not journal and arxiv:
        journal = f"arXiv preprint arXiv:{arxiv}"
    kind = normalize_type(entry_type, f)
    if kind == "misc" and not entry_type:
        kind = _classify(journal or "", " ".join(str(v) for v in f.values()), bool(title))
    return {"entry_type": kind, "title": title, "authors": authors, "year": year, "doi": doi,
            "journal": journal, "url": url}


# ---------------------------------------------------------------------------
# Entries -> rows
# ---------------------------------------------------------------------------

def convert_one(entry: dict) -> dict:
    """One entry of bibitem_parser.parse as one row (without arxiv_id)."""
    fields = entry.get("fields") or {}
    source = entry.get("source", "bibitem")
    has_named = bool(fields.get("title") or fields.get("author") or fields.get("journal"))
    row = from_fields(fields, entry.get("entry_type")) if (has_named or source != "bibitem") else {}
    if entry.get("raw") and (not row or any(row.get(n) in (None, "", []) for n in FIELDS[1:])):
        guessed = parse_free_text(entry["raw"], entry.get("label"))
        if not row:
            row = guessed
        else:  # fill what the named fields lack
            for name, value in guessed.items():
                if row.get(name) in (None, "", []) or (name == "entry_type" and row[name] == "misc"):
                    row[name] = value
    if not row:
        row = {"entry_type": "misc", "title": None, "authors": [], "year": None, "doi": None,
               "journal": None, "url": None}
    row["citation_key"] = entry["citation_key"].strip()
    row["entry_type"] = row.get("entry_type") or "misc"
    row["_source"] = "bibitem" if source == "bibitem" and not has_named else "structured"
    return {name: row.get(name) for name in FIELDS} | {"_source": row["_source"]}


def convert(entries: list[dict]) -> list[dict]:
    """Rows of the bibtex table (without arxiv_id) for one version's entries.
    An entry that cannot be read becomes a misc row with its key only (logged)."""
    rows, failed = [], 0
    for entry in entries:
        try:
            rows.append(convert_one(entry))
        except Exception as exc:  # one odd reference never fails the paper
            failed += 1
            log.warning("reference %r not converted: %s: %s", entry.get("citation_key"), type(exc).__name__, exc)
            rows.append({"citation_key": str(entry.get("citation_key", "")).strip(), "entry_type": "misc",
                         "title": None, "authors": [], "year": None, "doi": None, "journal": None,
                         "url": None, "_source": "bibitem"})
    LAST_STATS.clear()
    LAST_STATS.update(rows=len(rows), failed=failed,
                      with_title=sum(bool(r["title"]) for r in rows),
                      with_authors=sum(bool(r["authors"]) for r in rows),
                      with_year=sum(r["year"] is not None for r in rows),
                      with_doi=sum(bool(r["doi"]) for r in rows),
                      with_journal=sum(bool(r["journal"]) for r in rows),
                      with_url=sum(bool(r["url"]) for r in rows),
                      misc=sum(r["entry_type"] == "misc" for r in rows))
    return rows
