"""Member 2 - LaTeX cleanup (Milestone 2).

    clean(text) -> text

Removes from a whole document (gatherer.Gathered.text, comments already
gone) the commands that only lay out the page and carry no meaning, as the
statement asks (\\centering, the [htpb] placement option, \\midrule...):

    dropped with their arguments
        alignment     \\centering \\raggedright \\raggedleft \\flushleft ...
        spacing       \\vspace \\hspace \\vskip \\hfill \\noindent \\smallskip ...
        page breaks   \\newpage \\clearpage \\pagebreak \\FloatBarrier ...
        table rules   \\hline \\toprule \\midrule \\bottomrule \\cline \\cmidrule ...
        size, font    \\small \\footnotesize \\large ... \\bfseries \\itshape \\bf \\it ...
        lengths       \\setlength \\addtolength \\linespread \\fontsize ...
        colour        \\color \\rowcolor \\cellcolor ...
        settings      \\hypersetup \\graphicspath \\setcounter \\pagenumbering \\lstset ...
                      (written after \\begin{document} they would otherwise become sentences)
    unwrapped (the text is kept)
        \\textbf{x} \\emph{x} \\textit{x} \\underline{x} \\mbox{x} \\textcolor{c}{x}
        \\resizebox{w}{h}{x} \\scalebox{s}{x} ... and {\\bf x}, {\\em x}
    placement options  \\begin{figure}[htbp] -> \\begin{figure} (tables, floats)
    layout environments: \\begin and \\end of center, flushleft, minipage,
        adjustbox, multicols, spacing... are dropped, their content kept
    spacing characters ~ \\, \\; \\  -> a space; \\@ \\/ \\! -> nothing

Kept: everything hierarchy.py reads (sections, \\item, lists, abstract,
appendix, floats, bibliography), \\cite, \\ref, \\label, \\caption, \\footnote,
and every formula and literal part as it is (normalizer.math_spans): the
cleanup never edits inside $...$, an equation, verbatim or \\url.

A line left empty by a removal is removed with its end of line, so dropping
a \\vspace line never makes a blank line, which would cut a paragraph.
"""

from __future__ import annotations

import re

from latex_scan import read_group, read_optional
from normalizer import math_spans

LIMIT = 200_000  # how far an argument is looked for

ALIGNMENT = {"centering", "raggedright", "raggedleft", "flushleft", "flushright", "RaggedRight",
             "RaggedLeft", "Centering"}
SPACING = {"noindent", "indent", "smallskip", "medskip", "bigskip", "vfill", "hfill", "hfil", "vfil",
           "hss", "vss", "hrulefill", "dotfill", "null", "strut", "mathstrut", "quad", "qquad",
           "enspace", "thinspace", "negthinspace", "nobreak", "sloppy", "fussy", "nolinebreak",
           "allowbreak", "unskip", "relax"}
PAGE = {"newpage", "clearpage", "cleardoublepage", "pagebreak", "nopagebreak", "FloatBarrier",
        "flushbottom", "raggedbottom", "onecolumn", "balance", "tableofcontents", "listoffigures",
        "listoftables", "newline", "linebreak", "linenumbers", "nolinenumbers"}
RULES = {"hline", "toprule", "midrule", "bottomrule", "hdashline", "Xhline", "endhead", "endfirsthead",
         "endfoot", "endlastfoot", "tabularnewline"}
SIZES = {"tiny", "scriptsize", "footnotesize", "small", "normalsize", "large", "Large", "LARGE", "huge",
         "Huge"}
FONTS = {"bf", "it", "em", "rm", "sf", "tt", "sc", "sl", "bfseries", "itshape", "mdseries", "upshape",
         "slshape", "scshape", "rmfamily", "sffamily", "ttfamily", "normalfont", "boldmath", "unboldmath",
         "selectfont"}
SWITCHES = ALIGNMENT | SIZES | FONTS  # also seen as {\bf ...}: the group goes with the switch
DROP = SWITCHES | SPACING | PAGE | RULES
# command -> (optional arguments, brace arguments) to drop with it
DROP_ARGS = {"vspace": (0, 1), "hspace": (0, 1), "addvspace": (0, 1), "enlargethispage": (0, 1),
             "setlength": (0, 2), "addtolength": (0, 2), "linespread": (0, 1), "fontsize": (0, 2),
             "pagestyle": (0, 1), "thispagestyle": (0, 1), "captionsetup": (1, 1), "color": (1, 1),
             "rowcolor": (1, 1), "cellcolor": (1, 1), "columncolor": (1, 1), "arrayrulecolor": (1, 1),
             "rule": (1, 2), "cline": (0, 1), "cmidrule": (1, 1), "addlinespace": (1, 0),
             "specialrule": (0, 3), "rowcolors": (1, 3), "vspace*": (0, 1), "hspace*": (0, 1),
             "hypersetup": (0, 1), "graphicspath": (0, 1), "setcounter": (0, 2), "addtocounter": (0, 2),
             "pagenumbering": (0, 1), "urlstyle": (0, 1), "lstset": (0, 1), "sisetup": (0, 1)}
# command -> (optional arguments, brace arguments before the kept one)
UNWRAP = {"textbf": (0, 0), "textit": (0, 0), "emph": (0, 0), "texttt": (0, 0), "textsc": (0, 0),
          "textrm": (0, 0), "textsf": (0, 0), "textsl": (0, 0), "textup": (0, 0), "textmd": (0, 0),
          "textnormal": (0, 0), "underline": (0, 0), "uline": (0, 0), "uuline": (0, 0), "mbox": (0, 0),
          "hbox": (0, 0), "fbox": (0, 0), "makebox": (2, 0), "framebox": (2, 0), "textcolor": (1, 1),
          "colorbox": (1, 1), "fcolorbox": (1, 2), "resizebox": (0, 2), "scalebox": (1, 1),
          "raisebox": (2, 1), "textsuperscript": (0, 0)}
FLOATS = {"figure", "figure*", "table", "table*", "algorithm", "algorithm*", "sidewaysfigure",
          "sidewaystable", "SCfigure", "SCtable", "longtable", "longtable*"}
WRAP_FLOATS = {"wrapfigure": 2, "wraptable": 2, "wrapfloat": 3}  # brace arguments after [lines]
# environments that only lay out their content: (optional arguments, brace arguments) of \begin
LAYOUT_ENVS = {"center": (0, 0), "flushleft": (0, 0), "flushright": (0, 0), "raggedright": (0, 0),
               "Center": (0, 0), "FlushLeft": (0, 0), "FlushRight": (0, 0), "minipage": (3, 1),
               "adjustbox": (0, 1), "multicols": (0, 1), "multicols*": (0, 1), "spacing": (0, 1),
               "singlespace": (0, 0), "doublespace": (0, 0), "onehalfspace": (0, 0), "sloppypar": (0, 0),
               "landscape": (0, 0), "samepage": (0, 0), "small": (0, 0), "footnotesize": (0, 0),
               "scriptsize": (0, 0), "tiny": (0, 0), "large": (0, 0), "normalsize": (0, 0)}

_SCAN = re.compile(
    r"\{\s*\\(?P<switch>" + "|".join(sorted(SWITCHES, key=len, reverse=True)) + r")(?![A-Za-z@])"
    r"|\\(?P<be>begin|end)\s*\{(?P<env>[^{}]+)\}"
    r"|\\(?P<name>[A-Za-z@]+\*?)"
    r"|\\(?P<symbol>[^A-Za-z@])"
    r"|(?P<tie>~)", re.S)
_DIMEN = re.compile(r"\s*(?:-?(?:\d+\.?\d*|\.\d+)\s*(?:pt|mm|cm|in|em|ex|bp|pc|sp|dd|cc|mu)|\\[A-Za-z@]+)"
                    r"(?:\s*(?:plus|minus)\s*-?(?:\d+\.?\d*|\.\d+)\s*(?:pt|mm|cm|in|em|ex|fil+))*")
_SPACES = re.compile(r"[ \t]*")
_PAREN = re.compile(r"\s*\([^()\n]*\)")  # \cmidrule(lr)
_ARGUMENT_BEFORE = re.compile(r"(?:\\[A-Za-z@]+\*?|[\]}])\s*\Z")  # the { opens a command's argument
_HOLE = "\x02"   # where something was removed
_KEEP = re.compile("\x03(\\d+)\x03")
_EMPTIED_LINE = re.compile(r"(?m)^[ \t\x02]*\x02[ \t\x02]*(?:\n|\Z)")
_RUNS = re.compile(r"[ \t]{2,}")
SPACE_SYMBOLS = {",": " ", ";": " ", ":": " ", " ": " ", "/": "", "!": ""}


def _skip(text: str, end: int, optional: int, groups: int) -> int:
    """After up to `optional` [...] and then `groups` {...} arguments."""
    for _ in range(optional):
        found = read_optional(text, end, LIMIT)
        if not found:
            break
        end = found[1]
    for _ in range(groups):
        found = read_group(text, end, LIMIT)
        if not found:
            break
        end = found[1]
    return end


def _clean(text: str) -> str:
    """Cleanup of a text whose formulas and literal parts are placeholders."""
    out, pos = [], 0
    while True:
        m = _SCAN.search(text, pos)
        if m is None:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos:m.start()])
        pos = m.end()
        if m.group("switch"):  # {\bf text}: the text without the group
            group = read_group(text, m.start(), LIMIT)
            if group is None or _ARGUMENT_BEFORE.search(text, max(0, m.start() - 80), m.start()):
                # an argument such as \caption{\small ...}: only the switch goes
                out.append("{" + _HOLE)
                pos = _SPACES.match(text, m.end()).end()
                continue
            inner = group[0]
            inner = inner[inner.index(m.group("switch")) + len(m.group("switch")):]
            out.append(_clean(inner.lstrip(" \t")))
            pos = group[1]
        elif m.group("be"):
            env = m.group("env").strip()
            if env in LAYOUT_ENVS:
                if m.group("be") == "begin":
                    pos = _skip(text, pos, *LAYOUT_ENVS[env])
                out.append(_HOLE)
            elif m.group("be") == "begin" and env in FLOATS:
                out.append(m.group(0))
                pos = _skip(text, pos, 1, 0)  # [htbp]
            elif m.group("be") == "begin" and env in WRAP_FLOATS:
                out.append(m.group(0))
                pos = _skip(text, pos, 1, WRAP_FLOATS[env])
                pos = _skip(text, pos, 1, 0)  # the [overhang] after the width
            else:
                out.append(m.group(0))
        elif m.group("name"):
            name = m.group("name")
            bare = name.rstrip("*")
            if name == "@":  # \@ (spacing after a full stop): nothing
                continue
            if bare in DROP and name == bare:
                pos = _SPACES.match(text, pos).end()  # TeX skips the spaces after a control word
                out.append(_HOLE + " ")  # a\newline b, \quad: the words stay apart
            elif name in DROP_ARGS:
                if bare == "cmidrule":
                    paren = _PAREN.match(text, pos)
                    pos = paren.end() if paren else pos
                pos = _skip(text, pos, *DROP_ARGS[name])
                out.append(_HOLE)
            elif bare in ("vskip", "hskip", "kern", "hglue", "vglue"):
                dimen = _DIMEN.match(text, pos)
                pos = dimen.end() if dimen else pos
                out.append(_HOLE)
            elif name in UNWRAP:
                start = _skip(text, pos, *UNWRAP[name])
                group = read_group(text, start, LIMIT)
                if group is None:  # \textbf without its argument: left as it is
                    out.append(m.group(0))
                    continue
                out.append(_clean(group[0]))
                pos = group[1]
            else:
                out.append(m.group(0))
        elif m.group("symbol"):
            symbol = m.group("symbol")
            out.append(SPACE_SYMBOLS.get(symbol, m.group(0)))
        else:  # ~
            out.append(" ")


def clean(text: str) -> str:
    """`text` without layout commands (see the module docstring)."""
    text = text.replace("\x02", "").replace("\x03", "")  # the markers used below
    kept: list[str] = []

    def keep(span) -> str:
        kept.append(text[span.start:span.end])
        return f"\x03{len(kept) - 1}\x03"

    out, pos = [], 0
    for span in math_spans(text, literals=True):
        out.append(text[pos:span.start])
        out.append(keep(span))
        pos = span.end
    out.append(text[pos:])
    cleaned = _clean("".join(out))
    cleaned = _EMPTIED_LINE.sub("", cleaned).replace(_HOLE, "")
    cleaned = "\n".join(line.rstrip() for line in _RUNS.sub(" ", cleaned).split("\n"))
    return _KEEP.sub(lambda m: kept[int(m.group(1))], cleaned)
