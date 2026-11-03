"""Member 2 - Math normalization (Milestone 2).

    normalize(text) -> text

Gives every formula of a document one form, as the statement asks:

    inline   $...$, \\(...\\), \\begin{math}...\\end{math}, \\ensuremath{...}
             -> $...$
    display  $$...$$, \\[...\\], equation, equation*, displaymath, dmath
             -> \\begin{equation}...\\end{equation}
             align, align*, eqnarray, flalign, IEEEeqnarray (lines with & and \\\\)
             -> \\begin{equation}\\begin{aligned}...\\end{aligned}\\end{equation}
             alignat{n} -> ... \\begin{alignedat}{n} ...
             gather, multline -> ... \\begin{gathered} ... (lines without &)
             subequations, dgroup: the wrapper is dropped, the formulas in it
             are normalized one by one.

Renaming align to equation alone would not compile (an equation holds one
line), so lines keep their environment inside the equation. The content of a
formula is copied as it is: symbols, \\label, \\tag, \\nonumber are not touched.

Not math: \\$ (a dollar sign), verbatim environments, \\verb, \\url and \\href
(latex_scan.LITERAL). A delimiter that is never closed is left as text: $
looks for its closing $ in its paragraph, \\[ and $$ up to the next section
command, as hierarchy.py does.

math_spans(text) gives where the formulas are, for cleaner.py (which never
edits inside them) and leaf_extractor.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from latex_scan import LITERAL, find_env_end, literal_end, read_group

EQUATION_ENVS = {"equation", "equation*", "displaymath", "dmath", "dmath*"}
ALIGNED_ENVS = {"align", "align*", "eqnarray", "eqnarray*", "flalign", "flalign*",
                "IEEEeqnarray", "IEEEeqnarray*", "IEEEeqnarraybox"}
GATHERED_ENVS = {"gather", "gather*", "multline", "multline*"}
ALIGNEDAT_ENVS = {"alignat", "alignat*", "xalignat", "xxalignat"}
WRAPPER_ENVS = {"subequations", "dgroup", "dgroup*"}  # hold formulas, number them together
INLINE_ENVS = {"math"}
DISPLAY_ENVS = EQUATION_ENVS | ALIGNED_ENVS | GATHERED_ENVS | ALIGNEDAT_ENVS | WRAPPER_ENVS

_SECTIONING = r"\\(?:part|chapter|section|subsection|subsubsection)\*?(?![A-Za-z@])"
_TOKEN = re.compile(
    LITERAL +
    r"|\\begin\s*\{(?P<begin>[^{}]+)\}"
    r"|(?P<paren>\\\()|(?P<bracket>\\\[)"
    r"|(?P<ensure>\\ensuremath)(?![A-Za-z@])"
    r"|\\."
    r"|(?P<dollars>\$\$?)", re.S)
_PAREN_END = re.compile(r"\\\)|\\.", re.S)
_BRACKET_END = re.compile(r"\\\]|" + _SECTIONING + r"|\\.", re.S)
_DOUBLE_END = re.compile(r"\$\$|" + _SECTIONING + r"|\\.", re.S)
_SINGLE_END = re.compile(r"\$|\n[ \t]*\n|\\.", re.S)


@dataclass
class Span:
    start: int        # the formula with its delimiters: text[start:end]
    end: int
    inner: str        # what is between the delimiters
    display: bool
    env: str = ""     # the environment, "" for a delimiter
    arg: str = ""     # alignat's {n}, IEEEeqnarray's {rCl}


def _close(pattern: re.Pattern, closer: str, text: str, start: int) -> re.Match | None:
    """The closing delimiter after `start`, None when a stop (a section
    command, a blank line for $) comes first. Escaped characters are skipped."""
    for m in pattern.finditer(text, start):
        token = m.group(0)
        if token == closer:
            return m
        if token[0] != "\\" or len(token) > 2:  # a blank line or a section command
            return None
    return None


def math_spans(text: str, literals: bool = False) -> list[Span]:
    """Every formula of `text` in reading order, outermost only. The literal
    parts (verbatim, \\verb, \\url) are skipped, or given too with
    literals=True (env "literal", display False)."""
    spans: list[Span] = []
    pos = 0
    while True:
        m = _TOKEN.search(text, pos)
        if m is None:
            return spans
        pos = m.end()
        if m.group("env") or m.group("inline"):
            pos = max(literal_end(text, m), m.end())
            if literals:
                spans.append(Span(m.start(), pos, text[m.start():pos], False, "literal"))
            continue
        env = (m.group("begin") or "").strip()
        if env:
            if env not in DISPLAY_ENVS and env not in INLINE_ENVS:
                continue
            body, arg = m.end(), ""
            if env in ALIGNEDAT_ENVS or env.startswith("IEEEeqnarray"):
                group = read_group(text, body, 200)
                if group:
                    arg, body = group
            close = find_env_end(text, env, m.end())
            if close is None:
                continue
            spans.append(Span(m.start(), close[1], text[body:close[0]], env not in INLINE_ENVS, env, arg))
            pos = close[1]
        elif m.group("paren"):
            close = _close(_PAREN_END, "\\)", text, m.end())
            if close:
                spans.append(Span(m.start(), close.end(), text[m.end():close.start()], False))
                pos = close.end()
        elif m.group("bracket"):
            close = _close(_BRACKET_END, "\\]", text, m.end())
            if close:
                spans.append(Span(m.start(), close.end(), text[m.end():close.start()], True))
                pos = close.end()
        elif m.group("ensure"):
            group = read_group(text, m.end(), 2000)
            if group:
                spans.append(Span(m.start(), group[1], group[0], False, "ensuremath"))
                pos = group[1]
        elif m.group("dollars") == "$$":
            close = _close(_DOUBLE_END, "$$", text, m.end())
            if close:
                spans.append(Span(m.start(), close.end(), text[m.end():close.start()], True))
                pos = close.end()
        elif m.group("dollars") == "$":
            close = _close(_SINGLE_END, "$", text, m.end())
            if close:
                spans.append(Span(m.start(), close.end(), text[m.end():close.start()], False))
                pos = close.end()


def equation(inner: str) -> str:
    return "\\begin{equation}" + inner.strip("\n") + "\\end{equation}"


def _display(span: Span) -> str:
    inner = span.inner
    if span.env in WRAPPER_ENVS:
        return normalize(inner)
    if span.env in ALIGNED_ENVS:
        return equation("\\begin{aligned}" + inner + "\\end{aligned}")
    if span.env in ALIGNEDAT_ENVS:
        return equation("\\begin{alignedat}{" + span.arg + "}" + inner + "\\end{alignedat}")
    if span.env in GATHERED_ENVS:
        return equation("\\begin{gathered}" + inner + "\\end{gathered}")
    return equation(inner)


def normalize(text: str) -> str:
    """`text` with every formula in its standard form (module docstring)."""
    out, pos = [], 0
    for span in math_spans(text):
        out.append(text[pos:span.start])
        out.append(_display(span) if span.display else "$" + span.inner.strip() + "$")
        pos = span.end
    out.append(text[pos:])
    return "".join(out)
