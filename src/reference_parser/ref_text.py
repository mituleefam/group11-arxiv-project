"""Member 3 - Milestone 2: text helpers shared by the reference modules.

Nothing here knows about a paper or a pipeline: these are small, pure
functions on strings, used by bibitem_parser, bibtex_converter,
dedup_bibtex and citation_updater.

    read_braced(s, i)      the {...} group that starts at s[i] (nested, \\{ escaped)
    read_bracketed(s, i)   the [...] group that starts at s[i] (braces protect ])
    split_top(s, sep_re)   split at a separator that is outside every brace group
    strip_tex_comments(s)  remove % comments (not \\%, not inside \\url{...})
    latex_to_text(s)       LaTeX -> plain Unicode text ({\\"a} -> ä, \\& -> &, no braces)
    title_key(s)           a title reduced for comparison (lower case, no accents, a-z0-9)
    normalize_doi(s)       "https://doi.org/10.1/ABC." -> "10.1/abc"
    find_doi / find_arxiv_id / normalize_url

No third-party package is needed.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Brace groups
# ---------------------------------------------------------------------------


def read_braced(s: str, i: int) -> tuple[str, int] | None:
    """(content, end) of the brace group opening at s[i]; end is just after
    the closing brace. None when s[i] is not "{" or the group never closes."""
    if i >= len(s) or s[i] != "{":
        return None
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return None


def read_bracketed(s: str, i: int) -> tuple[str, int] | None:
    """(content, end) of the [...] group opening at s[i]; a ] inside braces
    does not close it ([{a]b}] is one group)."""
    if i >= len(s) or s[i] != "[":
        return None
    depth, j = 0, i + 1
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(depth - 1, 0)
        elif c == "]" and depth == 0:
            return s[i + 1:j], j + 1
        j += 1
    return None


def skip_spaces(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def split_top(s: str, sep: re.Pattern) -> list[str]:
    """Split `s` at every match of `sep` that is outside brace groups."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(depth - 1, 0)
        elif depth == 0:
            m = sep.match(s, i)
            if m and m.end() > i:
                parts.append(s[start:i])
                start = i = m.end()
                continue
        i += 1
    parts.append(s[start:])
    return parts


_URL_ARG = re.compile(r"\\(?:url|href|path|nolinkurl)\s*\{")
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")


def strip_tex_comments(s: str) -> str:
    """Remove % comments to the end of the line. \\% is a percent sign, and a
    % inside \\url{...} or \\href{...} (an escaped URL) is kept. Text that has
    no comment left (gatherer output) comes back unchanged."""
    if "%" not in s:
        return s
    out, pos = [], 0
    for m in _URL_ARG.finditer(s):
        group = read_braced(s, m.end() - 1)
        if group is None:
            continue
        out.append(_COMMENT.sub("", s[pos:m.start()]))
        out.append(s[m.start():group[1]])
        pos = group[1]
    out.append(_COMMENT.sub("", s[pos:]))
    return "".join(out)


# ---------------------------------------------------------------------------
# LaTeX -> text
# ---------------------------------------------------------------------------

_ACCENT_SYMBOL = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303",
                  "=": "\u0304", ".": "\u0307"}
_ACCENT_LETTER = {"u": "\u0306", "v": "\u030c", "H": "\u030b", "c": "\u0327", "k": "\u0328",
                  "r": "\u030a", "d": "\u0323", "b": "\u0331", "t": "\u0361"}
_SYMBOLS = {"ss": "ß", "o": "ø", "O": "Ø", "aa": "å", "AA": "Å", "ae": "æ", "AE": "Æ",
            "oe": "œ", "OE": "Œ", "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ", "dh": "ð", "DH": "Ð",
            "th": "þ", "TH": "Þ", "ng": "ŋ", "NG": "Ŋ", "dj": "đ", "DJ": "Đ"}
_ESCAPES = {"&": "&", "%": "%", "$": "$", "#": "#", "_": "_", " ": " ", ",": " ", ";": " ",
            ":": " ", "!": "", "-": "", "/": "", "@": ""}

# \'e  \'{e}  {\'e}  \' e  \'\i
_ACC1 = re.compile(r"\\([" + re.escape("".join(_ACCENT_SYMBOL)) + r"])\s*"
                   r"(?:\{\s*(\\[ij](?![A-Za-z])|[A-Za-z])\s*\}|(\\[ij](?![A-Za-z])|[A-Za-z]))")
# \c{c}  \v s  \u{a}
_ACC2 = re.compile(r"\\([" + "".join(_ACCENT_LETTER) + r"])(?:\s*\{\s*(\\[ij](?![A-Za-z])|[A-Za-z])\s*\}"
                   r"|\s+([A-Za-z]))")
_SYM = re.compile(r"\\(" + "|".join(sorted(_SYMBOLS, key=len, reverse=True)) + r")(?![A-Za-z])\s*(?:\{\})?")
_ESC = re.compile(r"\\([&%$#_ ,;:!\-/@])")
# commands whose argument is the text: \emph{x} -> x (the braces go later)
_KEEP_ARG = re.compile(r"\\(?:emph|textit|textbf|textsc|texttt|textrm|textsf|textsl|textup|textmd|"
                       r"textnormal|mathrm|mathit|mathbf|mathsf|mathtt|mathcal|mathbb|boldsymbol|"
                       r"bm|mbox|hbox|text|uppercase|lowercase|MakeUppercase|MakeLowercase|"
                       r"natexlab|bibnamefont|bibfnamefont|citenamefont|bibinfo\s*\{[^{}]*\}|"
                       r"bibfield\s*\{[^{}]*\}|enquote|textquotedblleft|ensuremath|url|nolinkurl|"
                       r"path|doi|eprint|arxiv)\*?\s*(?=\{)")
_HREF = re.compile(r"\\href\s*")
# commands that are dropped with their argument
_DROP_ARG = re.compile(r"\\(?:label|BibitemOpen|bibitemStop|BibitemShut|EndOfBibitem|"
                       r"selectlanguage|foreignlanguage\s*\{[^{}]*\}|bibAnnoteFile|"
                       r"providecommand|hskip|vskip|penalty|showISSN|showISBNx?|showLCCN|"
                       r"showDOI|showURL|showarticletitle|showeprint)\s*")
_LIGATURES = [("---", "\u2014"), ("--", "\u2013"), ("``", '"'), ("''", '"'), ("\\textendash", "\u2013"),
              ("\\textemdash", "\u2014"), ("\\ldots", "..."), ("\\dots", "..."), ("\\textquoteright", "'"),
              ("\\textquoteleft", "'"), ("\\copyright", "\u00a9"), ("\\textregistered", "\u00ae"),
              ("\\texttrademark", "\u2122")]
_COMMAND = re.compile(r"\\[A-Za-z@]+\*?")
_SPACES = re.compile(r"\s+")


def _accent(base: str, mark: str) -> str:
    base = {"\\i": "i", "\\j": "j"}.get(base, base)
    return unicodedata.normalize("NFC", base + mark)


def _drop_group(s: str, pattern: re.Pattern) -> str:
    out, pos = [], 0
    for m in pattern.finditer(s):
        if m.start() < pos:
            continue
        out.append(s[pos:m.start()])
        end = m.end()
        group = read_braced(s, skip_spaces(s, end))
        pos = group[1] if group else end
    out.append(s[pos:])
    return "".join(out)


def _href_text(s: str) -> str:
    """\\href{url}{text} -> text (the address is read before, by find_url)."""
    out, pos = [], 0
    for m in _HREF.finditer(s):
        if m.start() < pos:
            continue
        first = read_braced(s, m.end())
        second = read_braced(s, skip_spaces(s, first[1])) if first else None
        if not second:
            continue
        out.append(s[pos:m.start()])
        out.append(second[0])
        pos = second[1]
    out.append(s[pos:])
    return "".join(out)


def latex_to_text(s: str | None) -> str:
    """Plain text of a LaTeX fragment: accents become Unicode letters, escapes
    their character, formatting commands disappear and keep their argument,
    braces and $ go, spaces are collapsed."""
    if not s:
        return ""
    s = s.replace("\\{", "\x02").replace("\\}", "\x03")
    s = _ACC1.sub(lambda m: _accent(m.group(2) or m.group(3), _ACCENT_SYMBOL[m.group(1)]), s)
    s = _ACC2.sub(lambda m: _accent(m.group(2) or m.group(3), _ACCENT_LETTER[m.group(1)]), s)
    s = _SYM.sub(lambda m: _SYMBOLS[m.group(1)], s)
    for old, new in _LIGATURES:
        s = s.replace(old, new)
    s = _ESC.sub(lambda m: _ESCAPES[m.group(1)], s)
    s = s.replace("\\\\", " ")
    s = _drop_group(s, _DROP_ARG)
    s = _href_text(s)
    s = _KEEP_ARG.sub("", s)
    s = _COMMAND.sub(" ", s)   # \em, \bf, \newblock, unknown macros: the name goes, an argument stays
    s = s.replace("~", " ").replace("{", "").replace("}", "").replace("$", "")
    s = s.replace("\x02", "{").replace("\x03", "}")
    s = _SPACES.sub(" ", s).strip()
    # no space before punctuation left by a removed command ("Title \bf ." -> "Title.")
    s = re.sub(r"\s+([,.;:!?)\]])", r"\1", s)
    s = re.sub(r"([(\[])\s+", r"\1", s)
    return unicodedata.normalize("NFC", s)


def clean_field(s: str | None) -> str | None:
    """latex_to_text, then without the quotes and end punctuation that
    surround a field cut out of a sentence. None when nothing is left."""
    return strip_end(latex_to_text(s))


def strip_end(text: str) -> str | None:
    """Without the quotes and punctuation around a field cut out of a
    sentence; the period of an abbreviation ("Phys.") stays."""
    text = text.strip(" \t\n,;:\"'\u201c\u201d\u2018\u2019")
    if text.endswith(".") and not re.search(r"(?:^|[\s(])[A-Z][a-z]{0,4}\.$", text):
        text = text.rstrip(".").strip(" \t\n,;:\"'\u201c\u201d\u2018\u2019")
    text = re.sub(r"^(?:in|In|IN)\s*:\s*", "", text)
    return text or None


# ---------------------------------------------------------------------------
# Identifiers and keys for comparison
# ---------------------------------------------------------------------------

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s{}\"<>\\]+)", re.I)
_DOI_PREFIX = re.compile(r"^(?:https?://)?(?:dx\.)?(?:doi\.org/|doi:\s*)", re.I)
ARXIV_RE = re.compile(r"(?:arxiv(?:\.org/(?:abs|pdf)/|[\s:]*(?:e-?print[\s:]*)?(?:preprint[\s:]*)?(?:arxiv[\s:]*)?)"
                      r"|10\.48550/arxiv\.|corr[\s,]*(?:vol\.?\s*)?abs/)"
                      r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?", re.I)
URL_RE = re.compile(r"https?://[^\s{}\"<>\\]+", re.I)


def normalize_doi(s: str | None) -> str | None:
    """A DOI as compared and stored: no resolver prefix, no end punctuation,
    lower case (DOIs are case-insensitive)."""
    if not s:
        return None
    s = _DOI_PREFIX.sub("", s.strip())
    m = DOI_RE.search(s)
    if not m:
        return None
    doi = m.group(1).rstrip(".,;:)]}'")
    if doi.count("(") < doi.count(")"):
        doi = doi.rstrip(")")
    return doi.lower()


def find_doi(s: str) -> str | None:
    return normalize_doi(s) if s and "10." in s else None


def find_arxiv_id(s: str | None) -> str | None:
    """The arXiv identifier named in a string (no version): 1706.03762,
    hep-th/9901001."""
    if not s:
        return None
    m = ARXIV_RE.search(s)
    return m.group(1).lower() if m else None


def normalize_url(s: str | None) -> str | None:
    """A URL reduced for comparison: no scheme, no www, no end slash, arXiv
    pdf links as abs links. None when it names no page (a bare host)."""
    if not s:
        return None
    u = s.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.rstrip("/.,;")
    u = re.sub(r"^arxiv\.org/pdf/", "arxiv.org/abs/", u)
    u = re.sub(r"(arxiv\.org/abs/[^/]+?)(?:v\d+)?(?:\.pdf)?$", r"\1", u)
    if "/" not in u or len(u.split("/", 1)[1]) < 3:
        return None
    return u


_TITLE_CHARS = re.compile(r"[^a-z0-9]+")


def title_key(s: str | None) -> str:
    """A title as compared: no accents, lower case, letters and digits
    separated by single spaces."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _TITLE_CHARS.sub(" ", s.lower()).strip()


def surname_key(author: str | None) -> str:
    """The family name of an author stored as "Last, First", as compared."""
    if not author:
        return ""
    return title_key(author.split(",")[0]).replace(" ", "")
