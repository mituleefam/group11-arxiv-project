"""Milestone 3, Member 1 - text cleaning for reference matching.

Every value an entry (bibtex table of Milestone 2) is compared on with a
reference (Semantic Scholar metadata of Milestone 1) goes through the same
functions here, so the two sides end up in the same form:

    normalize_text("Attention Is All You Need")       -> "attention is all you need"
    title_clean("K\\"ahler-Einstein metrics: $U(1)$")  -> "kahler einstein metrics u 1"
    title_tokens("Attention is all you need")         -> ["attention", "need"]
    last_name("Vaswani, Ashish") == last_name("Ashish Vaswani") == "vaswani"
    find_arxiv_id("https://arxiv.org/abs/2406.05335v2")  -> "2406.05335"
    find_arxiv_id("10.48550/arXiv.2304.03010")           -> "2304.03010"
    find_arxiv_id("math.AG/0101001")                     -> "math/0101001"
    normalize_doi("https://doi.org/10.1073/PNAS.1117723109") -> "10.1073/pnas.1117723109"

Only the standard library is used.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# ---------------------------------------------------------------------------
# LaTeX to plain text
# ---------------------------------------------------------------------------

GREEK = ("alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi "
         "omicron pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega").split()
_GREEK_UNICODE = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "ϵ": "epsilon", "ζ": "zeta",
    "η": "eta", "θ": "theta", "ϑ": "theta", "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu", "µ": "mu",
    "ν": "nu", "ξ": "xi", "ο": "omicron", "π": "pi", "ρ": "rho", "ϱ": "rho", "σ": "sigma", "ς": "sigma",
    "τ": "tau", "υ": "upsilon", "φ": "phi", "ϕ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
}
# letters that NFKD does not split into a base letter and an accent
_SPECIAL_LETTERS = {"ß": "ss", "ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ł": "l",
                    "Ł": "l", "đ": "d", "Đ": "d", "ı": "i", "þ": "th", "ð": "d"}
_LETTER_COMMANDS = {"ss": "ss", "o": "o", "O": "o", "aa": "a", "AA": "a", "ae": "ae", "AE": "ae", "oe": "oe",
                    "OE": "oe", "l": "l", "L": "l", "i": "i", "j": "j"}
# \'e  \"{o}  \^ {a}  \c{c}  \v s ...: keep the letter, the accent goes with ASCII folding
_ACCENT_SYMBOL = re.compile(r"""\\['`^"~=.]\s*(?:\{\s*([A-Za-z])\s*\}|([A-Za-z]))""")
_ACCENT_LETTER = re.compile(r"\\[uvHcdbtkr](?:\s*\{\s*([A-Za-z])\s*\}|\s+([A-Za-z]))")
_LETTER_COMMAND = re.compile(r"\\(" + "|".join(sorted(_LETTER_COMMANDS, key=len, reverse=True)) + r")(?![A-Za-z])")
_GREEK_COMMAND = re.compile(r"\\(?:up|var)?(" + "|".join(sorted(set(GREEK), key=len, reverse=True)) + r")(?![A-Za-z])",
                            re.I)
_ESCAPED = re.compile(r"\\([&%$#_{}])")
_COMMAND = re.compile(r"\\[A-Za-z]+\*?|\\.")  # any other command: its name goes, its argument stays
_LATEX_CHARS = re.compile(r"[\\${}~]")


def latex_to_plain(text: str | None) -> str:
    """Text without LaTeX markup: accents become plain letters (\\"a -> a),
    Greek commands their names (\\alpha -> alpha), other command names are
    dropped and their arguments kept (\\emph{Deep} -> Deep), math delimiters,
    braces and ~ go. Commas are kept (author names need them)."""
    if not text:
        return ""
    s = str(text)
    if not _LATEX_CHARS.search(s):  # most titles and names: nothing to do
        return s
    s = _ESCAPED.sub(r" \1 ", s)
    s = _ACCENT_SYMBOL.sub(lambda m: m.group(1) or m.group(2), s)
    s = _ACCENT_LETTER.sub(lambda m: m.group(1) or m.group(2), s)
    s = _LETTER_COMMAND.sub(lambda m: _LETTER_COMMANDS[m.group(1)], s)
    s = _GREEK_COMMAND.sub(lambda m: " " + m.group(1).lower() + " ", s)
    s = _COMMAND.sub(" ", s)
    s = s.replace("$", " ").replace("{", "").replace("}", "").replace("~", " ")
    return s


def ascii_fold(text: str) -> str:
    """Kähler -> Kahler, Łukasz -> Lukasz, α -> alpha."""
    if text.isascii():
        return text
    out = []
    for ch in text:
        if ch in _GREEK_UNICODE:
            out.append(" " + _GREEK_UNICODE[ch] + " ")
        elif ch in _SPECIAL_LETTERS:
            out.append(_SPECIAL_LETTERS[ch])
        else:
            out.append(ch)
    decomposed = unicodedata.normalize("NFKD", "".join(out))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


_APOSTROPHES = re.compile(r"['’`´]")  # O'Brien -> obrien, "Dujmovi'c" (an accent of Semantic Scholar) -> dujmovic


@lru_cache(maxsize=1 << 18)
def _normalize(text: str) -> str:
    s = _APOSTROPHES.sub("", ascii_fold(latex_to_plain(text)).lower())
    return _NOT_ALNUM.sub(" ", s).strip()


def normalize_text(text: str | None) -> str:
    """Lower case ASCII words separated by one space: LaTeX removed, accents
    folded, every punctuation mark (hyphen, colon, quote...) a space."""
    return _normalize(str(text)) if text else ""


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

# Common English function words; small on purpose so that a title keeps its
# content words ("deep", "learning", "model" are not stop words here).
STOP_WORDS = frozenset("""
a an the and or nor but of in on at to for from by with without within into onto over under about
above below between among through during via per as is are was were be been being am it its this
that these those their there here which who whom whose what when where why how than then so such
not no do does did can could should would may might will shall must we our you your they them he
she his her i me my us also using use based towards toward new
""".split())


def title_clean(title: str | None) -> str | None:
    """The title as one normalized string, or None when there is no title."""
    s = normalize_text(title)
    return s or None


def title_tokens(title: str | None, remove_stop_words: bool = True) -> list[str]:
    """The words of the title. Stop words are dropped unless that would leave
    nothing ("To be or not to be" keeps all its words)."""
    words = normalize_text(title).split()
    if not remove_stop_words:
        return words
    kept = [w for w in words if w not in STOP_WORDS]
    return kept or words


# ---------------------------------------------------------------------------
# Author names
# ---------------------------------------------------------------------------

NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "2nd", "3rd", "phd"})
NOT_A_PERSON = frozenset({"others", "et al", "al", "etal", "and others", "et alii"})
GROUP_WORDS = frozenset({"collaboration", "collaborations", "consortium", "group", "team", "project",
                         "organization", "organisation", "institute", "association", "committee",
                         "society", "council", "inc", "ltd", "corporation", "foundation", "university",
                         "laboratory", "agency", "initiative", "network", "alliance", "commission"})


def _surname_words(name: str) -> list[str]:
    """Words of the surname part of one person's name, normalized."""
    plain = latex_to_plain(name).strip().rstrip("\\").strip()
    if "," in plain:          # "von Beethoven, Ludwig" or "King, Jr., Martin Luther": surname first
        surname = plain.split(",", 1)[0]
        words = normalize_text(surname).split()
    else:                     # "Ludwig van Beethoven", "J. Smith", "OpenAI"
        words = normalize_text(plain).split()
    while len(words) > 1 and words[-1] in NAME_SUFFIXES:
        words.pop()
    return words


def last_name(name: str | None) -> str | None:
    """The surname key of one author, the same for "Vaswani, Ashish" and
    "Ashish Vaswani": the last word of the surname, lower case ASCII
    ("van Beethoven, Ludwig" -> "beethoven", "Garcia-Perez, J." -> "perez").
    An organisation keeps its whole name ("ligo scientific collaboration").
    None for "others" / "et al." and for an empty name."""
    return _last_name(str(name)) if name else None


@lru_cache(maxsize=1 << 18)
def _last_name(name: str) -> str | None:
    whole = normalize_text(name)
    if not whole or whole in NOT_A_PERSON:
        return None
    if GROUP_WORDS & set(whole.split()):
        return whole
    words = _surname_words(name)
    return words[-1] if words else None


def split_authors(authors) -> list[str]:
    """One string per person. A BibTeX author string that holds several
    people separated by commas ("E. Ozkan, H. Aydin, R. Dikici": every part
    has a space and none looks like a lone given name) is split; "Last, First"
    is not."""
    out = []
    for a in authors or []:
        if a is None:
            continue
        parts = [p.strip() for p in str(a).split(",") if p.strip()]
        if len(parts) >= 3 and all(" " in p for p in parts):
            out.extend(parts)
        else:
            out.append(str(a))
    return out


def authors_last(authors) -> list[str]:
    """Surname keys of a list of authors, in order, without the ones that are
    not names (others, et al.)."""
    keys = []
    for a in split_authors(authors):
        k = last_name(a)
        if k:
            keys.append(k)
    return keys


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

OLD_ARCHIVES = ("acc-phys adap-org alg-geom ao-sci astro-ph atom-ph bayes-an chao-dyn chem-ph cmp-lg comp-gas "
                "cond-mat cs dg-ga funct-an gr-qc hep-ex hep-lat hep-ph hep-th math math-ph mtrl-th nlin nucl-ex "
                "nucl-th patt-sol physics plasm-ph q-alg q-bio q-fin quant-ph solv-int stat supr-con").split()
_OLD_ID = re.compile(r"(?<![a-z\-])((?:" + "|".join(sorted(OLD_ARCHIVES, key=len, reverse=True)) +
                     r")(?:\.[a-z]{2})?/\d{7})(?:v\d+)?(?!\d)", re.I)
_NEW_ID = re.compile(r"(?<!\d)(?<!\d\.)(\d{2})(\d{2})\.(\d{4,5})(?:v\d+)?(?!\d)")


def _valid_new_id(yy: str, mm: str, number: str) -> bool:
    """New-style ids start in April 2007 (0704); 5-digit numbers from 1501."""
    y, m = int(yy), int(mm)
    if not 1 <= m <= 12 or (y, m) < (7, 4):
        return False
    return len(number) == (5 if (y, m) >= (15, 1) else 4)


def normalize_arxiv_id(arxiv_id: str | None) -> str | None:
    """"2406.05335v2" -> "2406.05335", "math.AG/0101001" -> "math/0101001"
    (the form arXiv and Semantic Scholar use), "2511-23122" -> "2511.23122"
    (folder form of Milestone 1)."""
    if not arxiv_id:
        return None
    s = str(arxiv_id).strip().lower()
    s = re.sub(r"^arxiv:\s*", "", s)
    s = re.sub(r"v\d+$", "", s)
    m = re.fullmatch(r"(\d{4})[-.](\d{4,5})", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.fullmatch(r"([a-z\-]+)(?:\.[a-z]{2})?/(\d{7})", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return s or None


def find_arxiv_id(text: str | None) -> str | None:
    """The first arXiv id written in `text`, normalized, or None. An old id
    (hep-th/9901001) is taken anywhere; a new one (2406.05335) only when the
    text names arXiv (arxiv.org link, "arXiv:", arXiv DOI 10.48550/arXiv.) or
    CoRR ("CoRR abs/1810.04805"), so that a page range or a report number
    like 1234.5678 is not taken for an id."""
    if not text:
        return None
    s = str(text)
    old = _OLD_ID.search(s)
    if old:
        return normalize_arxiv_id(old.group(1))
    low = s.lower()
    if "arxiv" not in low and not ("corr" in low and "abs/" in low):
        return None
    for m in _NEW_ID.finditer(s):
        if _valid_new_id(m.group(1), m.group(2), m.group(3)):
            return f"{m.group(1)}{m.group(2)}.{m.group(3)}"
    return None


_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*[:=]?\s*)", re.I)
_DOI_IN_URL = re.compile(r"(?:dx\.)?doi\.org/(10\.\d{4,9}/\S+)", re.I)


def normalize_doi(doi: str | None) -> str | None:
    """Lower case DOI without its https://doi.org/ or doi: prefix and
    trailing punctuation; None when it does not start with 10."""
    if not doi:
        return None
    s = _DOI_PREFIX.sub("", str(doi).strip()).strip().rstrip(".,;)").lower()
    return s if s.startswith("10.") and "/" in s else None


def doi_from_url(url: str | None) -> str | None:
    """The DOI of a doi.org link, normalized."""
    m = _DOI_IN_URL.search(url or "")
    return normalize_doi(m.group(1)) if m else None


def year_of(value) -> int | None:
    """2017 from 2017, "2017", "2017-06-12T17:57:34Z"; None otherwise."""
    if value is None:
        return None
    m = re.match(r"\s*(\d{4})", str(value))
    if not m:
        return None
    y = int(m.group(1))
    return y if 1000 <= y <= 2100 else None
