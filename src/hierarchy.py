"""Member 1 - Hierarchy tree construction (Milestone 2).

Turns the document of one version (gatherer.Gathered.text, after Member 2's
cleaning and normalization and Member 3's citation renaming) into a tree,
and gives its edges for the `hierarchy` table:

    tree = build(text, root_id, registry, extract_leaves)
    tree.edges      [(element_id, parent_id), ...] in reading order

Nodes, as (kind, content) passed to Member 4's registry, which gives each its
element_id at once:
  document   the root, "Version {n} root", made by the caller (one per version)
  heading    \\part, \\chapter, \\section, \\subsection, \\subsubsection,
             starred or not: the title. Also "Abstract" (abstract
             environment), "Acknowledgements" (an acknowledgement section,
             environment or command) and "Appendix", made by \\appendix: the
             appendix sections are its children, so they can be found.
  paragraph  \\paragraph and \\subparagraph: the title
  list       itemize, enumerate, description and their paralist / enumitem
             variants: the environment name
  item       \\item: its [label] if any, else ""
  leaves     sentence, formula, figure: whatever Member 2's extract_leaves()
             returns for each paragraph of text (blank lines and \\par cut
             the text into paragraphs; a paragraph never spans a heading, a
             list or an item)
A node goes under the deepest open division: a \\subsection under the last
\\section, text under the last heading or item, a list under the item or
heading it is written in.

Left out of the tree: the preamble (the tree is the document body), the
bibliography (thebibliography and its variants, \\bibliography,
\\printbibliography: they are Member 3's), a section titled References or
Bibliography with all it holds, front matter commands (\\title, \\author,
\\maketitle...) and keyword environments, and macro definitions in the body
(\\newcommand{\\foo}{\\section{X}} is no section). A section command renamed by
a macro in the document (\\newcommand{\\sect}[1]{\\section{#1}}) is read as itself;
other macros are not expanded. tree.problems says when a document has no
heading or an \\item outside a known list, which usually means such macros.

Kept whole, never cut by a blank line nor searched for structure: floats
(figure, table, algorithm...), display math (equation, align, \\[ \\], $$ $$
...), verbatim, \\verb, \\url, tabular and tikzpicture; they reach extract_leaves
inside their paragraph. A \\[ or $$ never closes across a section command, so
an unbalanced one cannot hide the sections after it. Other environments (theorem, proof, center...) are
transparent: their text and lists are read as if the environment were not
there, and their \\begin / \\end are left to Member 2's cleaning.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from latex_scan import LITERAL, literal_end, read_group, read_optional

TITLE_LIMIT, FRONT_LIMIT = 2000, 200_000  # how far an argument is looked for
SECTION_LEVELS = {"part": -1, "chapter": 0, "section": 1, "subsection": 2, "subsubsection": 3,
                  "paragraph": 4, "subparagraph": 5}
PARAGRAPH_COMMANDS = {"paragraph", "subparagraph"}  # kind "paragraph"; the others are "heading"
TITLED_KINDS = {"heading", "paragraph"}
LIST_ENVS = {"itemize", "enumerate", "description", "itemize*", "enumerate*", "description*",
             "compactitem", "compactenum", "compactdesc", "asparaitem", "asparaenum", "asparadesc",
             "inparaitem", "inparaenum", "inparadesc", "list"}
SECTION_ENVS = {"abstract": "Abstract", "acknowledgements": "Acknowledgements",
                "acknowledgments": "Acknowledgements", "acknowledgement": "Acknowledgements",
                "acknowledgment": "Acknowledgements", "acks": "Acknowledgements", "ack": "Acknowledgements"}
OPAQUE_ENVS = {
    # floats
    "figure", "figure*", "table", "table*", "wrapfigure", "wraptable", "sidewaysfigure", "sidewaystable",
    "SCfigure", "SCtable", "algorithm", "algorithm*", "longtable", "longtable*", "subfigure", "subtable",
    # display math
    "equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "flalign",
    "flalign*", "alignat", "alignat*", "eqnarray", "eqnarray*", "displaymath", "math", "subequations",
    "dmath", "dmath*", "dgroup", "dgroup*",
    # other blocks kept whole
    "verbatim", "verbatim*", "Verbatim", "Verbatim*", "BVerbatim", "BVerbatim*", "LVerbatim", "LVerbatim*",
    "spverbatim", "lstlisting", "minted", "alltt", "tabular", "tabular*", "tabularx", "tabulary", "tabu",
    "xtabular", "supertabular", "tikzpicture", "tikzcd", "pgfpicture", "picture", "forest", "algorithmic",
    "algorithmic*", "algorithm2e", "IEEEeqnarray", "IEEEeqnarray*"}
BIB_ENVS = {"thebibliography", "thebibliography*", "references", "bibliography", "bibdiv", "biblist"}
DROPPED_ENVS = BIB_ENVS | {"keyword", "keywords", "IEEEkeywords"}
APPENDIX_ENVS = {"appendices", "appendix", "subappendices"}
FRONT_COMMANDS = {  # command -> number of brace arguments; they are not part of the tree
    "title": 1, "author": 1, "affiliation": 1, "affil": 1, "altaffiliation": 1, "address": 1,
    "email": 1, "date": 1, "thanks": 1, "keywords": 1, "institute": 1, "maketitle": 0,
    "titlerunning": 1, "authorrunning": 1, "icmltitle": 1, "icmlauthor": 2, "icmlaffiliation": 2,
    "icmlcorrespondingauthor": 2, "icmlkeywords": 1, "orcid": 1, "correspondingauthor": 1,
    "subjclass": 1, "pacs": 1, "ccsdesc": 1, "IEEEauthorblockN": 1, "IEEEauthorblockA": 1}
BIB_COMMANDS = ("bibliography", "bibliographystyle", "nobibliography", "addbibresource",
                "printbibliography")

ACK_TITLE = re.compile(r"^\W*acknowledge?ments?\W*$", re.I)
REFERENCES_TITLE = re.compile(r"^\W*(references?|bibliography|literature cited|works cited)\W*$", re.I)

_BEGIN_DOC = re.compile(r"\\begin\s*\{document\}")
_END_DOC = re.compile(r"\\end\s*\{document\}")
_BIB = re.compile(r"\\(?:" + "|".join(BIB_COMMANDS) + r")\*?(?![A-Za-z@])")
_SECTIONING = r"\\(?:part|chapter|section|subsection|subsubsection)\*?(?![A-Za-z@])"
DEFINITIONS = {"newcommand": 1, "renewcommand": 1, "providecommand": 1, "DeclareRobustCommand": 1,
               "newenvironment": 2, "renewenvironment": 2, "def": 1, "gdef": 1, "edef": 1, "xdef": 1,
               "titleformat": 0, "titlespacing": 0}  # command -> number of bodies
_MASKABLE = re.compile(
    LITERAL.replace("(?P<env>", "(?P<verbatim>")
    + r"|\\begin\s*\{(?P<env>[^{}]+)\}|(?P<dm>\\\[)"
    r"|\\(?P<defn>" + "|".join(sorted(DEFINITIONS, key=len, reverse=True)) + r")\*?(?![A-Za-z@])"
    r"|\\let\s*\\[A-Za-z@]+\s*=?\s*\\[A-Za-z@]+"
    r"|\\.|(?P<dollars>\$\$?)", re.S)
_BRACKET_END = re.compile(r"\\\]|" + _SECTIONING + r"|\\.", re.S)
_DOUBLE_END = re.compile(r"\$\$|" + _SECTIONING + r"|\\.", re.S)
_SINGLE_END = re.compile(r"\$|\n[ \t]*\n|\\.", re.S)
_SECTION_CMD = re.compile(_SECTIONING)
_NAME = re.compile(r"\s*(?:\\[A-Za-z@]+|\\.)")
_ALIAS = re.compile(r"\\(?:(?:new|renew|provide)command\*?\s*\{?\\([A-Za-z@]+)\}?|[gex]?def\s*\\([A-Za-z@]+))\s*"
                    r"(?:\[1\]|#1)?\s*\{\s*\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)"
                    r"(\*?)\s*(?:\{\s*#1\s*\})?\s*\}")
_STRUCT = re.compile(
    r"\\(?P<sec>part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?(?![A-Za-z@])"
    r"|\\(?P<be>begin|end)\s*\{(?P<env>[^{}]+)\}"
    r"|\\(?P<item>item)(?![A-Za-z@])"
    r"|\\(?P<appendix>appendix|appendices)(?![A-Za-z@])"
    r"|\\(?P<abstract>abstract)(?![A-Za-z@])"
    r"|\\(?P<ackcmd>acknowledgments|acknowledgements|acknowledgment|acknowledgement)(?![A-Za-z@])"
    r"|\\(?P<front>" + "|".join(sorted(FRONT_COMMANDS, key=len, reverse=True)) + r")(?![A-Za-z@])")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*(?:\n[ \t]*)+|\\par(?![A-Za-z@])")
_BRACES = re.compile(r"\\par(?![A-Za-z@])|\\.|[{}]|\n[ \t]*(?:\n[ \t]*)+", re.S)
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")


def split_paragraphs(text: str) -> list[str]:
    """Cut at blank lines and \\par, but only outside braces: a blank line in
    a \\footnote{...} does not cut its argument in two."""
    if "{" not in text:
        return _PARAGRAPH_BREAK.split(text)
    pieces, depth, start = [], 0, 0
    for m in _BRACES.finditer(text):
        token = m.group(0)
        if token == "{":
            depth += 1
        elif token == "}":
            depth = max(depth - 1, 0)
        elif token[0] == "\\" and token != "\\par":
            continue  # an escaped character (\{ \} \\) or another command
        elif depth == 0:  # a blank line or \par outside braces
            pieces.append(text[start:m.start()])
            start = m.end()
    pieces.append(text[start:])
    return pieces


@dataclass
class Tree:
    edges: list[tuple[str, str]] = field(default_factory=list)  # (element_id, parent_id)
    kinds: Counter = field(default_factory=Counter)             # kind of every child, per edge
    problems: list[str] = field(default_factory=list)


@dataclass
class _Frame:
    type: str            # root, heading, appendix, env, list, item
    level: float         # sectioning level (part -1 ... subparagraph 5); 99 for the others
    node: str | None     # element_id; None while a "References" heading has no child
    parent: str | None = None
    kind: str = ""
    content: str = ""
    env: str = ""


def document_body(text: str) -> str:
    """Between \\begin{document} and \\end{document} (all of it without them)."""
    begin = _BEGIN_DOC.search(text)
    if begin is None:
        return text
    end = _END_DOC.search(text, begin.end())
    return text[begin.end():end.start() if end else len(text)]


def remove_bibliography(text: str) -> str:
    """Without \\bibliography{...}, \\bibliographystyle{...},
    \\printbibliography[...] and the like (the thebibliography environment
    is removed by mask())."""
    out, pos = [], 0
    for m in _BIB.finditer(text):
        if m.start() < pos:
            continue
        out.append(text[pos:m.start()])
        end = m.end()
        optional = read_optional(text, end, TITLE_LIMIT)
        if optional:
            end = optional[1]
        group = read_group(text, end, TITLE_LIMIT)
        if group and not m.group(0).startswith("\\printbibliography"):
            end = group[1]
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _env_ends(text: str, env: str) -> dict[int, int]:
    """Where each closed `env` ends: end of its \\begin -> end of its \\end. One
    pass per environment name, so many open environments cost no more than one."""
    ends: dict[int, int] = {}
    stack: list[int] = []
    for m in re.finditer(r"\\(begin|end)\s*\{\s*" + re.escape(env) + r"\s*\}", text):
        if m.group(1) == "begin":
            stack.append(m.end())
        elif stack:
            ends[stack.pop()] = m.end()
    return ends


def _math_end(pattern: re.Pattern, closer: str, text: str, start: int) -> tuple[int | None, bool]:
    """End of a \\[ or $$ opened before `start`, stopping at a section command;
    and whether the search reached the end of the text without a stop."""
    for m in pattern.finditer(text, start):
        token = m.group(0)
        if token == closer:
            return m.end(), False
        if len(token) > 2:  # a section command
            return None, False
    return None, True


def _definition_end(text: str, m: re.Match) -> int | None:
    """End of the macro definition `m` starts, or None if it does not read as one."""
    command, end = m.group("defn"), m.end()
    if command.endswith("def"):  # \\def\\name#1#2{body}
        name = _NAME.match(text, end)
        if name is None:
            return None
        brace = text.find("{", name.end(), name.end() + 200)
        group = read_group(text, brace, FRONT_LIMIT) if brace >= 0 else None
        return group[1] if group else None
    group = read_group(text, end, TITLE_LIMIT)  # the name: {\\name} or \\name
    if group:
        end = group[1]
    else:
        name = _NAME.match(text, end)
        if name is None:
            return None
        end = name.end()
    if not DEFINITIONS[command]:  # \\titleformat{\\section}...: only hide the section command
        return end
    optional = read_optional(text, end, TITLE_LIMIT)
    while optional:
        end = optional[1]
        optional = read_optional(text, end, TITLE_LIMIT)
    for _ in range(DEFINITIONS[command]):
        group = read_group(text, end, FRONT_LIMIT)
        if group is None:
            return None
        end = group[1]
    return end


def mask(text: str) -> tuple[str, list[str]]:
    """Replace every block kept whole (OPAQUE_ENVS, \\[ \\], $$ $$, \\verb...) by a
    placeholder, and every DROPPED_ENVS block and macro definition by nothing.
    Returns the masked text and the blocks, placeholder k standing for blocks[k].
    Inline math is walked over, so the $$ of $a$$b$ opens no display math."""
    blocks: list[str] = []
    out, pos, scan = [], 0, 0
    env_ends: dict[str, dict[int, int]] = {}
    no_end: set[str] = set()  # closers known to be absent from the rest of the text
    while True:
        m = _MASKABLE.search(text, scan)
        if m is None:
            break
        env, dollars, drop = m.group("env"), m.group("dollars"), False
        if m.group("verbatim") or m.group("inline"):
            end = literal_end(text, m)
            end = end if end > m.end() or m.group("verbatim") else None
        elif env is not None:
            env = env.strip()
            if env not in OPAQUE_ENVS and env not in DROPPED_ENVS:
                scan = m.end()
                continue
            if env not in env_ends:
                env_ends[env] = _env_ends(text, env)
            end = env_ends[env].get(m.end())
            drop = env in DROPPED_ENVS
            if end is None and env in BIB_ENVS:  # never closed: up to the next section, or the end
                next_section = _SECTION_CMD.search(text, m.end())
                end = next_section.start() if next_section else len(text)
        elif m.group("dm"):
            end, reached_end = (None, False) if "]" in no_end else _math_end(_BRACKET_END, "\\]", text, m.end())
            if reached_end:
                no_end.add("]")
        elif dollars == "$$":
            end, reached_end = (None, False) if "$$" in no_end else _math_end(_DOUBLE_END, "$$", text, m.end())
            if reached_end:
                no_end.add("$$")
        elif dollars == "$":  # inline math: only walked over, it ends within its paragraph
            scan = m.end()
            for close in _SINGLE_END.finditer(text, m.end()):
                if close.group(0) == "$":
                    scan = close.end()
                    break
                if close.group(0)[0] == "\n":
                    break
            continue
        elif m.group("defn"):
            end, drop = _definition_end(text, m), True
        elif m.group(0).startswith("\\let") and len(m.group(0)) > 2:
            end, drop = m.end(), True
        else:
            scan = m.end()
            continue
        if end is None:  # not closed: left as text
            scan = m.end()
            continue
        out.append(text[pos:m.start()])
        if not drop:
            out.append(f"\x00{len(blocks)}\x00")
            blocks.append(text[m.start():end])
        pos = scan = end
    out.append(text[pos:])
    return "".join(out), blocks


class _Builder:
    def __init__(self, root_id: str, registry, extract_leaves, blocks: list[str]):
        self.root_id = root_id
        self.registry = registry
        self.extract = extract_leaves
        self.blocks = blocks
        self.frames = [_Frame("root", -9, root_id)]
        self.tree = Tree()
        self._count: Counter = Counter()
        self.buffer: list[str] = []
        self.appendix: _Frame | None = None
        self.top_level: float | None = None
        self.dropping: float | None = None  # level of the References section being left out
        self.item_reported = False

    # -- nodes --------------------------------------------------------------------------
    def new_node(self, kind: str, content: str, parent: str) -> str:
        content = " ".join(content.split())
        # A title is the same element wherever it moves between versions; the same title twice
        # in one version stays two nodes, or a \subsection{Results} in a \section{Results} would
        # be its own parent. A list or an item has no content of its own: it is known by its place.
        key_parent = None if parent == self.root_id or kind in TITLED_KINDS else parent
        key = (key_parent, kind, content)
        occurrence = self._count[key]
        self._count[key] += 1
        node = self.registry.element_id(kind, content, key_parent, occurrence)
        self.tree.edges.append((node, parent))
        self.tree.kinds[kind] += 1
        return node

    def container(self) -> str:
        """The node new content goes under (a pending heading is made now)."""
        return self.frames[-1].node

    def flush(self) -> None:
        """Leaves of the text read since the last structural command."""
        if not self.buffer:
            return
        text = "".join(self.buffer)
        self.buffer.clear()
        if not text.strip():
            return
        parent = None
        for paragraph in split_paragraphs(text):
            if not paragraph.strip():
                continue
            if "\x00" in paragraph:
                paragraph = _PLACEHOLDER.sub(lambda m: self.blocks[int(m.group(1))], paragraph)
            for kind, content in self.extract(paragraph):
                if content and content.strip():
                    parent = parent or self.container()
                    self.new_node(kind, content, parent)

    # -- structure ------------------------------------------------------------------------
    def pop_to_root(self) -> None:
        del self.frames[1:]

    def heading(self, level: int, command: str, title: str) -> None:
        self.flush()
        if self.dropping is not None and not ACK_TITLE.match(title):
            if level > self.dropping:
                return
        self.dropping = None
        if REFERENCES_TITLE.match(title):
            self.dropping = level
            return
        is_ack = bool(ACK_TITLE.match(title))
        if not is_ack:
            self.top_level = level if self.top_level is None else min(self.top_level, level)
        if is_ack:
            self.pop_to_root()
        else:
            if self.appendix is not None and level <= self.appendix.level + 0.5 \
                    and self.appendix not in self.frames:
                self.pop_to_root()
                self.frames.append(self.appendix)
            while len(self.frames) > 1:
                top = self.frames[-1]
                if top.type in ("heading", "appendix") and top.level < level:
                    break
                if top.type in ("list", "item", "env"):
                    self.tree.problems.append(f"\\{command}{{{title[:40]}}} inside an open {top.env or top.type}")
                self.frames.pop()
        parent = self.container()
        kind = "paragraph" if command in PARAGRAPH_COMMANDS else "heading"
        self.frames.append(_Frame("heading", level, self.new_node(kind, title, parent), parent, kind, title))

    def start_appendix(self) -> None:
        self.flush()
        self.dropping = None
        self.pop_to_root()
        if self.appendix is None:
            level = (self.top_level if self.top_level is not None else 1) - 0.5
            self.appendix = _Frame("appendix", level, self.new_node("heading", "Appendix", self.root_id))
        self.frames.append(self.appendix)

    def section_env(self, env: str, begin: bool) -> None:
        self.flush()
        if begin:  # front or back matter: under the root, whatever is open
            self.dropping = None
            node = self.new_node("heading", SECTION_ENVS[env], self.root_id)
            self.frames.append(_Frame("env", 99, node, env=env))
            return
        for i in range(len(self.frames) - 1, 0, -1):
            if self.frames[i].type == "env" and self.frames[i].env == env:
                del self.frames[i:]
                return
        self.tree.problems.append(f"\\end{{{env}}} without \\begin")

    def list_env(self, env: str, begin: bool) -> None:
        self.flush()
        if begin:
            node = self.new_node("list", env, self.container())
            self.frames.append(_Frame("list", 99, node, env=env))
            return
        for i in range(len(self.frames) - 1, 0, -1):
            if self.frames[i].type == "list" and self.frames[i].env == env:
                del self.frames[i:]
                return
        self.tree.problems.append(f"\\end{{{env}}} without \\begin")

    def item(self, label: str) -> bool:
        """An \\item; False when no list is open (the caller keeps it as text)."""
        for i in range(len(self.frames) - 1, 0, -1):
            if self.frames[i].type == "list":
                self.flush()
                del self.frames[i + 1:]  # the previous item ends here
                node = self.new_node("item", label, self.frames[i].node)
                self.frames.append(_Frame("item", 99, node))
                return True
        if not self.item_reported:
            self.item_reported = True
            self.tree.problems.append("\\item outside a known list (a list made by a macro?)")
        return False


def _skip_arguments(text: str, end: int, groups: int) -> int:
    optional = read_optional(text, end, TITLE_LIMIT)
    while optional:
        end = optional[1]
        optional = read_optional(text, end, TITLE_LIMIT)
    for _ in range(groups):
        group = read_group(text, end, FRONT_LIMIT)
        if group is None:
            break
        end = group[1]
    return end


def rename_section_macros(text: str, body: str) -> str:
    """`body` with the section commands renamed by a macro in `text` read as themselves."""
    aliases = {}
    for new, old, command, star in _ALIAS.findall(text):
        name = new or old
        if name not in SECTION_LEVELS:
            aliases.setdefault(name, f"\\{command}{star}")
    if not aliases:
        return body
    pattern = r"\\(" + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True)) + r")(?![A-Za-z@])"
    return re.sub(pattern, lambda m: aliases[m.group(1)], body)


def build(text: str, root_id: str, registry, extract_leaves) -> Tree:
    """The tree of one version's document under the node `root_id` (see the
    module docstring). `registry` and `extract_leaves` follow m2_contract."""
    text = text.replace("\r\n", "\n").replace("\x00", "")  # \x00 marks the blocks
    masked, blocks = mask(remove_bibliography(rename_section_macros(text, document_body(text))))
    b = _Builder(root_id, registry, extract_leaves, blocks)
    pos = 0
    for m in _STRUCT.finditer(masked):
        if m.start() < pos:
            continue  # inside the arguments of the previous command
        if b.dropping is None:
            b.buffer.append(masked[pos:m.start()])
        pos = m.end()
        if m.group("sec"):
            command = m.group("sec")
            short = read_optional(masked, pos, TITLE_LIMIT)
            title = read_group(masked, short[1] if short else pos, TITLE_LIMIT)
            if title is None:  # \section used as a word (\let, \renewcommand...): text
                if b.dropping is None:
                    b.buffer.append(m.group(0))
                continue
            pos = title[1]
            b.heading(SECTION_LEVELS[command], command, _PLACEHOLDER.sub(
                lambda x: blocks[int(x.group(1))], title[0]).strip())
        elif m.group("be"):
            env, begin = m.group("env").strip(), m.group("be") == "begin"
            if b.dropping is not None and env not in SECTION_ENVS and env not in APPENDIX_ENVS:
                continue
            if env in APPENDIX_ENVS:
                if begin:
                    b.start_appendix()
            elif env in LIST_ENVS:
                if begin:  # a list's own arguments: \begin{enumerate}[(a)], \begin{list}{label}{style}
                    pos = _skip_arguments(masked, pos, 2 if env == "list" else 0)
                b.list_env(env, begin)
            elif env in SECTION_ENVS:
                b.section_env(env, begin)
            else:
                b.buffer.append(m.group(0))  # transparent environment
        elif b.dropping is not None and not (m.group("appendix") or m.group("ackcmd")):
            if m.group("front"):
                pos = _skip_arguments(masked, pos, FRONT_COMMANDS[m.group("front")])
        elif m.group("item"):
            label = read_optional(masked, pos, TITLE_LIMIT)
            text_label = _PLACEHOLDER.sub(lambda x: blocks[int(x.group(1))], label[0]).strip() if label else ""
            if b.item(text_label):
                pos = label[1] if label else pos
            else:
                b.buffer.append(m.group(0))
        elif m.group("appendix"):
            b.start_appendix()
        elif m.group("ackcmd"):
            b.heading(SECTION_LEVELS["section"], "section", "Acknowledgements")
        elif m.group("abstract"):
            group = read_group(masked, pos, FRONT_LIMIT)
            if group is None:
                b.buffer.append(m.group(0))
            else:
                pos = group[1]
                b.section_env("abstract", True)
                b.buffer.append(group[0])
                b.section_env("abstract", False)
        else:
            pos = _skip_arguments(masked, pos, FRONT_COMMANDS[m.group("front")])
    if b.dropping is None:
        b.buffer.append(masked[pos:])
    b.flush()
    if b.tree.edges and not any(b.tree.kinds[k] for k in ("heading", "paragraph")):
        b.tree.problems.append("no heading in the document (sections made by a macro?)")
    return b.tree
