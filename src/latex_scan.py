"""Fast scanners for LaTeX source (Milestone 2, shared by gatherer.py and
hierarchy.py, Member 1).

They expand no macro and typeset nothing. They remove what TeX itself never
reads, and they find where a brace group, an optional argument or an
environment ends, so the other modules can work on the text LaTeX compiles.
Every function is linear in the length of the text: a regular expression
jumps from one character that matters (%, a backslash, a brace) to the next.

    strip_comments(text) -> (text, problems)
        Removes comments as TeX does: a % that is not escaped (\\% is a
        percent sign, but \\\\% is a line break followed by a comment) up to
        the end of the line, the end of line itself and the spaces that open
        the next line, so "a%<newline>b" gives "ab" and a line holding only a
        comment does not become an empty line (which would end a paragraph).
        Also removes \\iffalse ... \\fi (the \\else part is kept) and comment
        environments; verbatim environments, \\verb and \\lstinline are kept
        as they are, since a % or an \\input inside them is plain text. So is
        the address in \\url{...} and \\href{...}{...} (a % in it is a character).
    read_group(text, i, limit)    -> (content, end) of the {...} at i, or None
    read_optional(text, i, limit) -> (content, end) of the [...] at i, or None
        limit: how far to look, so a group left open costs little.
    find_env_end(text, name, i) -> (start, end) of the \\end{name} matching
        a \\begin{name} just before i, or None
"""

from __future__ import annotations

import re

# Environments whose body is copied as it is (a % or a command in them is text).
VERBATIM_ENVS = ("verbatim", "verbatim*", "Verbatim", "Verbatim*", "BVerbatim", "BVerbatim*",
                 "LVerbatim", "LVerbatim*", "spverbatim", "lstlisting", "minted", "alltt",
                 "filecontents", "filecontents*")
# \if... commands of etoolbox/ifthen are macros with brace arguments, not TeX
# conditionals: they have no \fi, so they are not counted when matching one.
MACRO_IFS = {"ifthenelse", "ifboolexpr", "ifbool", "iftoggle", "ifdef", "ifndef", "ifundef",
             "ifcsdef", "ifcsundef", "ifdefempty", "ifcsempty", "ifdefvoid", "ifcsvoid",
             "ifdefstring", "ifcsstring", "ifdefequal", "ifcsequal", "ifstrequal", "ifstrempty",
             "ifblank", "ifnumcomp", "ifnumequal", "ifnumgreater", "ifnumless", "ifnumodd",
             "ifdimcomp", "ifdimequal", "ifdimgreater", "ifdimless", "ifdefmacro", "ifdefparam",
             "ifdefprefix", "ifdefprotected", "ifdefltxprotect", "ifdefcounter", "ifdeflength",
             "ifdefdimen", "ifinlist", "ifinlistcs", "ifrmnum", "ifpackageloaded", "ifclassloaded",
             "iffileexists"}

_VERB_ENV_ALT = "|".join(re.escape(name) for name in VERBATIM_ENVS + ("comment",))
# One alternative per character sequence that matters; everything between two
# matches is plain text. A control symbol (\% \\ \{ ...) is consumed whole, so
# the % of \% is never a comment and the one after \\ is.
LITERAL = (r"\\(?:(?P<inline>verb\*?|lstinline|url|path|nolinkurl|href)(?![A-Za-z@])"
           r"|begin[ \t]*\{(?P<env>" + _VERB_ENV_ALT + r")\})")
_SPECIAL = re.compile(
    r"%"
    r"|" + LITERAL +
    r"|\\(?:(?P<cond>if[A-Za-z@]*|fi|else)(?![A-Za-z@])"
    r"|[^A-Za-z@])")
_CONTROL_WORD_END = re.compile(r"\\[A-Za-z@]+$")


def _comment_end(text: str, at: int) -> tuple[int, bool]:
    """Where reading goes on after the % at `at`, and whether the line after
    it is blank: past the end of line and the next line's leading spaces,
    or at the end of line when the next line is blank (it must stay blank)."""
    eol = text.find("\n", at)
    if eol < 0:
        return len(text), False
    nxt = eol + 1
    while nxt < len(text) and text[nxt] in " \t":
        nxt += 1
    blank = nxt >= len(text) or text[nxt] in "\r\n"
    return (eol, True) if blank else (nxt, False)


def literal_end(text: str, m: re.Match) -> int:
    """End of what match `m` of LITERAL starts: a verbatim environment (the end
    of the text when it is not closed), or the argument of \\verb, \\lstinline,
    \\url and the like, on one line; m.end() when there is no such argument."""
    env = m.groupdict().get("verbatim") or m.group("env")  # hierarchy.py names the group "verbatim"
    if env:
        close = text.find("\\end{" + env + "}", m.end())
        return len(text) if close < 0 else close + len(env) + 6
    command, at = m.group("inline"), m.end()
    if command == "lstinline":
        optional = read_optional(text, at, 500)
        if optional:
            at = optional[1]
    elif not command.startswith("verb"):  # \\url{...}: only a brace argument, maybe after a space
        at = skip_spaces(text, at)
        if not text.startswith("{", at):
            return m.end()
    if at >= len(text) or text[at] in " \t\n":
        return m.end()
    closing = "}" if text[at] == "{" else text[at]
    close = text.find(closing, at + 1)
    if close < 0 or text.find("\n", at + 1, close) >= 0:  # it ends on its line
        return m.end()
    return close + 1


def _matching_fi(text: str, start: int) -> tuple[int, int, bool] | None:
    """For an \\iffalse ending at `start`: (where its false part ends, where
    reading goes on, whether it ended at \\else), or None without a \\fi."""
    depth, pos = 0, start
    while True:
        m = _SPECIAL.search(text, pos)
        if m is None:
            return None
        token = m.group(0)
        if token == "%":
            pos = _comment_end(text, m.start())[0]
            continue
        if m.group("env") or m.group("inline"):
            pos = literal_end(text, m)
            continue
        cond = m.group("cond")
        if cond == "fi":
            if depth == 0:
                return m.start(), m.end(), False
            depth -= 1
        elif cond == "else" and depth == 0:
            return m.start(), m.end(), True
        elif cond and cond.startswith("if") and cond not in MACRO_IFS:
            depth += 1
        pos = m.end()


def strip_comments(text: str) -> tuple[str, list[str]]:
    """The text without what TeX does not read (see the module docstring),
    and a list of problems met (an \\iffalse without \\fi, an open verbatim)."""
    out: list[str] = []
    problems: list[str] = []
    depths: list[int] = []  # one per open \else part of an \iffalse: \if...\fi nested in it
    pos = 0
    while True:
        m = _SPECIAL.search(text, pos)
        if m is None:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        token = m.group(0)
        if token == "%":
            pos, blank_next = _comment_end(text, m.start())
            if not blank_next and pos < len(text):
                last = out[-1] if out[-1] else (out[-2] if len(out) > 1 else "")
                if _CONTROL_WORD_END.search(last):
                    out.append(" ")  # "\foo%<newline>bar" is \foo then bar, not \foobar
            continue
        env = m.group("env")
        if env or m.group("inline"):
            end = literal_end(text, m)
            if env and end == len(text) and not text.endswith("\\end{" + env + "}"):
                problems.append(f"\\begin{{{env}}} without \\end{{{env}}}")
            if env != "comment":
                out.append(text[m.start():end])
            pos = end
            continue
        cond = m.group("cond")
        if cond == "iffalse":
            found = _matching_fi(text, m.end())
            if found is None:
                problems.append("\\iffalse without \\fi: kept as text")
                out.append(token)
                pos = m.end()
                continue
            _, pos, at_else = found
            if at_else:
                depths.append(0)  # read the \else part; drop its \fi
            continue
        if depths and cond:
            if cond == "fi":
                if depths[-1] == 0:
                    depths.pop()
                    pos = m.end()
                    continue
                depths[-1] -= 1
            elif cond.startswith("if") and cond not in MACRO_IFS:
                depths[-1] += 1
        out.append(token)
        pos = m.end()
    if depths:
        problems.append("\\iffalse ... \\else without \\fi")
    return "".join(out), problems


_GROUP_CHARS = re.compile(r"\\.|[{}]", re.S)
_OPTIONAL_CHARS = re.compile(r"\\.|[{}\[\]]", re.S)
_SPACES = re.compile(r"[ \t]*(?:\r?\n[ \t]*)?")  # spaces and at most one end of line


def skip_spaces(text: str, i: int) -> int:
    """After the spaces TeX skips before an argument (not a blank line)."""
    return _SPACES.match(text, i).end()


def read_group(text: str, i: int, limit: int | None = None) -> tuple[str, int] | None:
    """(content, end) of the brace group starting at `i` after spaces, or
    None when there is none or it is not closed within `limit` characters.
    Escaped braces \\{ \\} do not count."""
    i = skip_spaces(text, i)
    if i >= len(text) or text[i] != "{":
        return None
    depth = 0
    for m in _GROUP_CHARS.finditer(text, i, len(text) if limit is None else i + limit):
        char = m.group(0)
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:m.start()], m.end()
    return None


def read_optional(text: str, i: int, limit: int | None = None) -> tuple[str, int] | None:
    """(content, end) of an optional argument [...] at `i` after spaces, or
    None. A ] inside braces does not close it."""
    i = skip_spaces(text, i)
    if i >= len(text) or text[i] != "[":
        return None
    depth = 0
    for m in _OPTIONAL_CHARS.finditer(text, i + 1, len(text) if limit is None else i + limit):
        char = m.group(0)
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(depth - 1, 0)
        elif char == "]" and depth == 0:
            return text[i + 1:m.start()], m.end()
    return None


_ENV_PATTERNS: dict[str, re.Pattern] = {}


def find_env_end(text: str, name: str, i: int) -> tuple[int, int] | None:
    """(start, end) of the \\end{name} closing an environment whose
    \\begin{name} ends at `i`, counting nested \\begin{name}; None if open."""
    pattern = _ENV_PATTERNS.get(name)
    if pattern is None:
        pattern = _ENV_PATTERNS[name] = re.compile(
            r"\\(begin|end)[ \t]*\{" + re.escape(name) + r"\}")
    depth = 1
    for m in pattern.finditer(text, i):
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return m.start(), m.end()
    return None
