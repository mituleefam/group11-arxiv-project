"""Member 2 - Sentence splitting for scientific LaTeX prose (Milestone 2).

    split_sentences(text) -> list[str]

A sentence ends at . ! or ? (with the closing quotes or brackets after it)
followed by a space and the start of a new sentence: a capital letter, a
digit, a quote, a bracket, a formula or a command. The statement asks not to
cut inside mathematics, commands, abbreviations such as i.e. and et al. and
Fig. 1, URLs or decimal numbers, so these are hidden (masked) before the
text is cut and put back after:

    formulas and literal parts   $...$, \\(...\\), equation..., \\verb, \\url (normalizer.math_spans)
    commands with arguments      \\cite{smith.2020}, \\ref{sec.1}, \\label{fig.1}, \\footnote{...},
                                 \\includegraphics[...]{fig.png}: the whole command
    web and identifiers          http://..., www...., names@host.org, 10.1016/j.x.2020
    numbers                      3.14, 0.001, v1.2, 1.2.3, .5
    abbreviations                e.g. i.e. et al. cf. vs. Fig. Eq. Sec. Tab. Ref. Dr. Prof. no. vol.
                                 pp. approx. resp. w.r.t. (ABBREVIATIONS), dotted acronyms
                                 (U.S., a.k.a.), an initial before a name (J. Smith), ...

"etc." is not hidden: it ends a sentence when a capital follows. A piece
with no letter outside commands and their arguments (a lone \\label{x}, a
full stop left after a formula) is no sentence and is dropped. Spaces in a
sentence are collapsed to one.
"""

from __future__ import annotations

import re

from latex_scan import read_group, read_optional
from normalizer import math_spans

ABBREVIATIONS = (
    "e.g.", "i.e.", "et al.", "cf.", "vs.", "viz.", "approx.", "resp.", "w.r.t.", "a.k.a.", "ca.",
    "Fig.", "Figs.", "fig.", "figs.", "Eq.", "Eqs.", "eq.", "eqs.", "Sec.", "Secs.", "sec.", "Sect.",
    "Tab.", "Tabs.", "tab.", "Ref.", "Refs.", "ref.", "refs.", "Thm.", "Lem.", "Prop.", "Def.", "Cor.",
    "Alg.", "Ex.", "Ch.", "Chap.", "App.", "Appx.", "Eqn.", "Eqns.", "Exp.", "Expt.", "Fn.",
    "No.", "no.", "Nos.", "Vol.", "vol.", "Vols.", "pp.", "p.", "ed.", "eds.", "Ed.", "Eds.", "Ph.D.",
    "Dr.", "Prof.", "Mr.", "Mrs.", "Ms.", "Jr.", "Sr.", "St.", "Inc.", "Ltd.", "Co.", "Corp.",
    "Dept.", "dept.", "Univ.", "univ.", "Jan.", "Feb.", "Mar.", "Apr.", "Jun.", "Jul.", "Aug.", "Sep.",
    "Sept.", "Oct.", "Nov.", "Dec.", "est.", "max.", "min.", "avg.", "std.", "var.", "eqn.", "cp.")
LIMIT = 50_000  # how far a command argument is looked for

_HIDE = re.compile(
    r"(?P<command>\\[A-Za-z@]+\*?)"
    r"|\\[^A-Za-z@]"                                            # \% \{ \\: one symbol
    r"|(?:https?|ftp)://[^\s{}]+|www\.[^\s{}]+"                 # URLs
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"                            # e-mail addresses
    r"|\b10\.\d{4,9}/[^\s{}]+"                                  # DOIs
    r"|\d*\.\d+(?:\.\d+)*"                                      # 3.14 .5 1.2.3 (v1.2 keeps its v)
    r"|\.{2,}"                                                  # ellipsis
    r"|(?<![A-Za-z])(?:" + "|".join(re.escape(a) for a in sorted(ABBREVIATIONS, key=len, reverse=True))
    + r")"
    r"|(?<![A-Za-z])(?:[A-Za-z]\.){2,}"                         # U.S. a.k.a. e.g.
    r"|(?<![A-Za-z])[A-Z]\.(?=[ \t\n]*[A-Z][a-z])")             # J. Smith
_SAVED = re.compile("\x01(\\d+)\x01")
_END = re.compile(r"[.!?]+[\"'\u201d\u2019)\]]*(?:\x01\d+\x01)*(?=\s+[A-Z0-9\x01\"'\u201c\u2018(\[$\\])")
_COMMAND = re.compile(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])*(?:\{[^{}]*\})*")
_LETTER = re.compile(r"[A-Za-z\u00C0-\u024F]")


def command_end(text: str, end: int) -> int:
    """After the optional and brace arguments that follow a command."""
    while True:
        found = read_optional(text, end, LIMIT) or read_group(text, end, LIMIT)
        if not found:
            return end
        end = found[1]


def _mask(text: str, saved: list[str]) -> str:
    def keep(piece: str) -> str:
        saved.append(piece)
        return f"\x01{len(saved) - 1}\x01"

    out, pos = [], 0
    for span in math_spans(text, literals=True):
        out.append(text[pos:span.start])
        out.append(keep(text[span.start:span.end]))
        pos = span.end
    out.append(text[pos:])
    text, out, pos = "".join(out), [], 0
    while True:
        m = _HIDE.search(text, pos)
        if m is None:
            out.append(text[pos:])
            return "".join(out)
        end = command_end(text, m.end()) if m.group("command") else m.end()
        # 'et al.' is often mid-sentence, but can finish a complete sentence.
        # Only allow splitting before a strong new-sentence opener to avoid
        # breaking author subjects such as 'Smith et al. introduced ...'.
        if text[m.start():end].lower() == "et al." and re.match(
                r"\s+(?:The|This|These|Those|We|Our|However|Moreover|"
                r"Furthermore|Therefore|Nevertheless|In contrast|It is|Such)\b",
                text[end:]):
            out.append(text[pos:end])
            pos = end
            continue
        out.append(text[pos:m.start()])
        out.append(keep(text[m.start():end]))
        pos = end


def _is_sentence(piece: str) -> bool:
    return bool(_LETTER.search(_COMMAND.sub("", piece)))


def split_sentences(text: str) -> list[str]:
    """The sentences of a piece of prose, in reading order (module docstring)."""
    text = text.replace("\x01", "")
    saved: list[str] = []
    masked = _mask(text, saved)
    sentences, pos = [], 0
    for m in _END.finditer(masked):
        sentences.append(masked[pos:m.end()])
        pos = m.end()
    sentences.append(masked[pos:])
    out = []
    for piece in sentences:
        while "\x01" in piece:  # a hidden command can hold a hidden formula: \footnote{$x.y$}
            piece = _SAVED.sub(lambda m: saved[int(m.group(1))], piece)
        piece = " ".join(piece.split())
        if piece and _is_sentence(piece):
            out.append(piece)
    return out
