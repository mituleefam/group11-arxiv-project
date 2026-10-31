"""Member 1 - Multi-file gathering (Milestone 2).

For one version folder of the Milestone 1 corpus (corpus/<yymm-id>/vN/, which
holds only the .tex and .bib files of the source), find the file LaTeX
compiles (the publication root), follow its \\input / \\include chain in
reading order, and give the document as one text, as the PDF was compiled:

    g = gather(Path("data/corpus/2604-05535/v1"))
    g.status     "ok", or why there is no document (see below)
    g.root       "main.tex"
    g.files      ["main.tex", "sec/intro.tex", ...]    the compilation path
    g.text       the whole document: every include replaced by its file,
                 without comments, \\iffalse parts or comment environments
    g.bib_files  the .bib files named by \\bibliography / \\addbibresource (a document
                 declaring no references gets the folder's .bib files: g.bib_fallback)
    g.unused     the .tex files of the folder that are not compiled
    g.missing    include targets that are not in the folder
    g.embedded_bib  .bib files written in the source by \\begin{filecontents}: name -> text
    g.problems   what is worth a look (several roots, a cycle, a guess...)

How the files are chosen, after how TeX and arXiv read them:
  * Root: a .tex file that no other file includes and that has, or includes,
    \\documentclass (or LaTeX 2.09's \\documentstyle) outside comments. When
    there are several (a figure made with the standalone class, a subfile, a
    cover letter, an old draft), come in this order, over what each one
    compiles: a class other than standalone/subfiles, \\begin{document}, a
    usual name (main, ms, paper...), the top folder, the most files, the largest.
    Without any \\documentclass, the only .tex file, or else the largest file
    with \\begin{document}, or else the largest one nobody includes, is taken
    and the guess is reported.
  * Includes: \\input{f}, \\input f, \\include{f}, \\InputIfFileExists{f},
    \\subfile{f}, and \\import{dir}{f}, \\subimport{dir}{f} with the other
    commands of the import package. A name can use \\jobname or a macro defined
    as plain text (\\newcommand{\\dir}{sections}, \\def\\dir{sections}).
    ".tex" is tried first when the name has
    no .tex extension, as TeX does. A name is looked for from the top of the
    version folder first (arXiv: "compilation is always done from the root of
    your submission directory"), then from the folder of the file that
    includes it, then from the root's folder; it can never leave the version
    folder. \\includeonly is honoured.
  * What TeX does not read is not followed: comments, \\iffalse ... \\fi,
    comment environments (latex_scan.strip_comments), anything after
    \\endinput; verbatim text is copied as it is.
  * An included file that is a document itself (\\documentclass and
    \\begin{document}, e.g. a subfile) gives only its body.
  * A file is never included inside itself (cycles), and depth and size are
    bounded.

status is "ok" for a document; "no_source" when Milestone 1 found no LaTeX
source for the version (pdf_only, no_tex, not_found, failed or missing
folder); "no_tex" when the folder has no .tex file. Both give an empty text:
the version gets an empty structure, never an error.

    python src/gatherer.py --data-dir data --survey   # every version, see survey()
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import posixpath
import re
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from latex_scan import LITERAL, literal_end, read_group, read_optional, skip_spaces, strip_comments

OK, NO_SOURCE, NO_TEX = "ok", "no_source", "no_tex"
USUAL_ROOT_NAMES = ("main", "ms", "paper", "manuscript", "article", "root", "arxiv", "submission")
MAX_DEPTH = 30                 # nested includes
MAX_CHARS = 30_000_000         # characters of one flattened document
CHILD_CLASSES = ("standalone", "subfiles")  # documents made to be included in another one

_CLASS = re.compile(r"\\document(?:class|style)(?![A-Za-z@])\s*(?:\[[^\]]*\])?\s*\{\s*([^}\s]*)\s*\}")
_BEGIN_DOC = re.compile(r"\\begin\s*\{document\}")
_END_DOC = re.compile(r"\\end\s*\{document\}")
_ENDINPUT = re.compile(r"\\endinput(?![A-Za-z@])")
_INCLUDE = re.compile(
    LITERAL + r"|\\(?P<cmd>input|include|subfile|InputIfFileExists|import|subimport|inputfrom|includefrom|"
    r"subinputfrom|subincludefrom|includeonly|bibliography|nobibliography|addbibresource|"
    r"addglobalbib|addsectionbib)\*?(?![A-Za-z@])")
_PRIMITIVE_NAME = re.compile(r"[^\s{}%\\]+")
_FILECONTENTS = re.compile(r"\\begin[ \t]*\{filecontents\*?\}(?:\[[^\]\n]*\])?\s*\{([^{}\n]*)\}(.*?)"
                           r"\\end\{filecontents\*?\}", re.S)
_TEXT_MACRO = re.compile(r"\\(?:(?:new|renew|provide)command\*?\s*\{?\\([A-Za-z@]+)\}?|[gex]?def\s*\\([A-Za-z@]+))"
                         r"\s*\{([^{}#\\\n]*)\}")
_MACRO_USE = re.compile(r"\\([A-Za-z@]+)\s*(?:\{\})?")
NAME_LIMIT, CODE_LIMIT = 2000, 200_000  # how far an argument is looked for
TWO_ARGUMENTS = {"import", "subimport", "inputfrom", "includefrom", "subinputfrom", "subincludefrom"}
BIB_COMMANDS = {"bibliography", "nobibliography", "addbibresource", "addglobalbib", "addsectionbib"}


@dataclass
class Gathered:
    status: str
    root: str | None = None
    files: list[str] = field(default_factory=list)
    text: str = ""
    bib_files: list[str] = field(default_factory=list)
    bib_names_missing: list[str] = field(default_factory=list)  # \bibliography{x} without x.bib
    bbl_input: bool = False  # \input{x.bbl}: references in a file Milestone 1 did not keep
    embedded_bib: dict[str, str] = field(default_factory=dict)
    bib_fallback: bool = False  # bib_files are the folder's .bib files: the document declares none
    has_thebibliography: bool = False
    unused: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OK

    def summary(self) -> dict:
        """Everything but the text (for logs and parsed/gathering.json)."""
        data = asdict(self)
        data.pop("text")
        data["embedded_bib"] = sorted(self.embedded_bib)
        data["chars"] = len(self.text)
        return data


def read_text(path: Path) -> tuple[str, bool]:
    """File content and whether it was UTF-8 (otherwise read as Latin-1,
    which keeps every byte)."""
    raw = path.read_bytes()
    try:
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text, utf8 = raw.decode("utf-16"), True
        else:
            text, utf8 = raw.decode("utf-8-sig"), True
    except UnicodeDecodeError:
        text, utf8 = raw.decode("latin-1"), False
    return text.replace("\r\n", "\n").replace("\r", "\n"), utf8  # old Mac files end lines with \r


class _Folder:
    """The files of one version folder, read and stripped once each."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.paths: dict[str, Path] = {}
        for directory, dirs, names in os.walk(self.folder):
            dirs.sort()
            for name in sorted(names):
                full = Path(directory) / name
                self.paths[full.relative_to(self.folder).as_posix()] = full
        self.lower = {rel.lower(): rel for rel in self.paths}
        self.tex = [rel for rel in self.paths if rel.lower().endswith(".tex")]
        self._stripped: dict[str, str] = {}
        self.embedded: dict[str, str] = {}  # \begin{filecontents}{name}: name -> text
        self.problems: list[str] = []

    def stripped(self, rel: str) -> str:
        """The file as TeX reads it: no comments, nothing after \\endinput."""
        text = self._stripped.get(rel)
        if text is None:
            raw, utf8 = read_text(self.paths[rel])
            if not utf8:
                self.problems.append(f"{rel}: not UTF-8, read as Latin-1")
            text, problems = strip_comments(raw)
            self.problems += [f"{rel}: {p}" for p in problems]
            if "filecontents" in text:  # a file written by the source is not part of the document
                for name, content in _FILECONTENTS.findall(text):
                    self.embedded.setdefault(name.strip(), content)
                text = _FILECONTENTS.sub("", text)
            m = _ENDINPUT.search(text)
            if m:  # TeX still reads the rest of that line
                eol = text.find("\n", m.end())
                text = text[:m.start()] + (text[m.end():eol] if eol >= 0 else text[m.end():])
            self._stripped[rel] = text
        return text

    def find(self, name: str, bases: list[str], tex: bool) -> tuple[str | None, bool]:
        """Relative path of `name` looked for from each base folder in turn,
        and whether only a case-insensitive match was found."""
        name = name.strip().strip('"').strip()
        if not name or "#" in name or "\\" in name:  # a macro argument or a command: unknown
            return None, False
        if tex and not name.lower().endswith(".tex"):
            candidates = [name + ".tex", name]
        elif not tex and not name.lower().endswith(".bib"):
            candidates = [name + ".bib", name]
        else:
            candidates = [name]
        case_hit = None
        for base in bases:
            for candidate in candidates:
                rel = posixpath.normpath(posixpath.join(base, candidate))
                if rel.startswith("../") or rel == ".." or rel.startswith("/"):
                    continue  # never outside the version folder
                if rel in self.paths:
                    return rel, False
                if case_hit is None and rel.lower() in self.lower:
                    case_hit = self.lower[rel.lower()]
        return (case_hit, True) if case_hit else (None, False)


@dataclass
class _Include:
    cmd: str
    start: int
    end: int
    names: list[str]
    directory: str = ""


def _parse_includes(text: str) -> list[_Include]:
    """Every include, bibliography and \\includeonly command of a stripped
    file, with its arguments, in order."""
    found = []
    pos = 0
    while True:
        m = _INCLUDE.search(text, pos)
        if m is None:
            return found
        if m.group("cmd") is None:  # \verb, a verbatim environment: an \input in it is plain text
            pos = max(literal_end(text, m), m.end())
            continue
        cmd, end, names, directory = m.group("cmd"), m.end(), [], ""
        if cmd == "input":
            group = read_group(text, end, NAME_LIMIT)
            if group is not None:
                names, end = [group[0]], group[1]
            else:  # TeX's own syntax: \input file.tex
                at = skip_spaces(text, end)
                word = _PRIMITIVE_NAME.match(text, at)
                if word:
                    names, end = [word.group(0)], word.end()
        elif cmd in TWO_ARGUMENTS:
            first = read_group(text, end, NAME_LIMIT)
            second = read_group(text, first[1], NAME_LIMIT) if first else None
            if second:
                directory, names, end = first[0].strip(), [second[0]], second[1]
        else:
            optional = read_optional(text, end, NAME_LIMIT) if cmd in ("addbibresource", "addglobalbib",
                                                                       "addsectionbib") else None
            group = read_group(text, optional[1] if optional else end, NAME_LIMIT)
            if group is not None:
                end = group[1]
                if cmd in ("bibliography", "nobibliography", "includeonly"):
                    names = [n for n in (part.strip() for part in group[0].split(",")) if n]
                else:
                    names = [group[0]]
                if cmd == "InputIfFileExists":  # its two code arguments are not text
                    for _ in range(2):
                        extra = read_group(text, end, CODE_LIMIT)
                        if extra is None:
                            break
                        end = extra[1]
        found.append(_Include(cmd, m.start(), end, names, directory))
        pos = max(end, m.end())


def _body(text: str) -> str | None:
    """What is between \\begin{document} and \\end{document}, or None."""
    begin = _BEGIN_DOC.search(text)
    if begin is None:
        return None
    end = _END_DOC.search(text, begin.end())
    return text[begin.end():end.start() if end else len(text)]


class _Gatherer:
    def __init__(self, folder: _Folder):
        self.f = folder
        self.result = Gathered(OK)
        self.only: set[str] | None = None  # \includeonly names
        self.size = 0
        self._macros: dict[str, str] | None = None

    # -- includes ---------------------------------------------------------------
    def plain_name(self, name: str, root: str | None) -> str:
        """An include's name with \\jobname and macros defined as plain text replaced."""
        if "\\" not in name:
            return name
        if self._macros is None:
            self._macros = {}
            for rel in self.f.tex:
                for new, old, value in _TEXT_MACRO.findall(self.f.stripped(rel)):
                    self._macros.setdefault(new or old, value.strip())
        job = posixpath.splitext(posixpath.basename(root or self.result.root or ""))[0]
        return _MACRO_USE.sub(lambda m: job if m.group(1) == "jobname"
                              else self._macros.get(m.group(1), m.group(0)), name)

    def targets(self, rel: str, inc: _Include, root: str | None = None) -> list[tuple[str, str | None, bool]]:
        """(name, resolved path or None, case-insensitive match) of an include
        of file `rel` (`root`: the root file, by default the chosen one)."""
        here = posixpath.dirname(rel)
        root_dir = posixpath.dirname(root or self.result.root or rel)
        out = []
        for name in inc.names:
            if inc.cmd in TWO_ARGUMENTS:
                joined = posixpath.join(self.plain_name(inc.directory, root), self.plain_name(name, root))
                bases = [here, ""] if inc.cmd.startswith("sub") else ["", here]
                path, case = self.f.find(joined, bases, tex=True)
            else:
                path, case = self.f.find(self.plain_name(name, root), ["", here, root_dir],
                                         tex=inc.cmd not in BIB_COMMANDS)
            out.append((name, path, case))
        return out

    def expand(self, rel: str, stack: tuple[str, ...]) -> str:
        """The text of `rel` with its includes replaced, recursively."""
        if rel not in self.result.files:
            self.result.files.append(rel)
        text = self.f.stripped(rel)
        if len(stack) > 1 and _CLASS.search(text):  # an included document: its body only
            body = _body(text)
            if body is not None:
                text = body
        out, pos = [], 0
        for inc in _parse_includes(text):
            out.append(text[pos:inc.start])
            pos = inc.end
            if inc.cmd in BIB_COMMANDS:
                for name, path, case in self.targets(rel, inc):
                    plain = self.plain_name(name, None).strip()
                    embedded = next((k for k in (plain, plain + ".bib") if k in self.f.embedded), None)
                    if embedded:
                        self.result.embedded_bib[embedded] = self.f.embedded[embedded]
                    elif path and path not in self.result.bib_files:
                        self.result.bib_files.append(path)
                    elif not path and name not in self.result.bib_names_missing:
                        self.result.bib_names_missing.append(name)
                out.append(text[inc.start:inc.end])  # left for the bibliography module
                continue
            if inc.cmd == "includeonly":
                self.only = {n.strip() for n in inc.names}
                continue
            if not inc.names:
                self.result.problems.append(f"{rel}: \\{inc.cmd} without a file name")
                continue
            if inc.cmd == "include" and self.only is not None and inc.names[0].strip() not in self.only:
                continue  # left out by \includeonly
            for name, path, case in self.targets(rel, inc):
                if path is None:
                    if self.plain_name(name, None).strip().lower().endswith(".bbl"):
                        self.result.bbl_input = True
                    elif "#" in name or "\\" in name:  # \input{#1} in a macro, \input{\dir/x}
                        self.result.problems.append(f"{rel}: \\{inc.cmd}{{{name}}} names a macro, not read")
                    elif name not in self.result.missing:
                        self.result.missing.append(name)
                    continue
                if case:
                    self.result.problems.append(f"{rel}: '{name}' found as {path} (case differs)")
                if path in stack:
                    self.result.problems.append(f"{rel}: cycle, {path} includes itself")
                    continue
                if len(stack) >= MAX_DEPTH or self.size > MAX_CHARS:
                    self.result.problems.append(f"{rel}: {path} not read (too deep or too large)")
                    continue
                if path in self.result.files and inc.cmd != "include":
                    self.result.problems.append(f"{rel}: {path} is read a second time")
                content = self.expand(path, stack + (path,))
                self.size += len(content)
                # \include starts a new page, so a new paragraph; \input inserts the text as it is
                out.append(f"\n\n{content}\n\n" if inc.cmd == "include" else content)
        out.append(text[pos:])
        return "".join(out)

    # -- root ---------------------------------------------------------------------
    def includes_of(self, rel: str) -> set[str]:
        """Files `rel` includes directly, as if it were the root."""
        found = set()
        for inc in _parse_includes(self.f.stripped(rel)):
            if inc.cmd not in BIB_COMMANDS and inc.cmd != "includeonly":
                found |= {path for _, path, _ in self.targets(rel, inc, root=rel) if path}
        return found

    def closure(self, rel: str, graph: dict[str, set[str]]) -> set[str]:
        seen, todo = {rel}, [rel]
        while todo:
            for nxt in graph.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    def choose_root(self) -> str | None:
        tex = self.f.tex
        if not tex:
            return None
        graph = {rel: self.includes_of(rel) for rel in tex}
        included = {path for rel, found in graph.items() for path in found if path != rel}
        info = {}
        for rel in tex:
            text = self.f.stripped(rel)
            cls = _CLASS.search(text)
            info[rel] = (cls.group(1) if cls else None, bool(_BEGIN_DOC.search(text)))
        candidates = [rel for rel in tex if info[rel][0] is not None]
        if candidates:
            reach = {rel: self.closure(rel, graph) for rel in tex if rel not in included}
            # a file nobody includes that has \documentclass or includes the file that has it
            roots = [rel for rel, seen in reach.items() if any(info[r][0] is not None for r in seen)] or candidates

            def score(rel):
                seen = reach.get(rel, {rel})
                classes = [info[r][0] for r in seen if info[r][0] is not None]
                stem = posixpath.splitext(posixpath.basename(rel))[0].lower()
                return (any(c not in CHILD_CLASSES for c in classes), any(info[r][1] for r in seen),
                        stem in USUAL_ROOT_NAMES, "/" not in rel, len(seen),
                        self.f.paths[rel].stat().st_size, rel)

            root = max(roots, key=score)
            if len(candidates) > 1:
                others = ", ".join(sorted(set(candidates) - {root}))
                self.result.problems.append(f"{len(candidates)} files with \\documentclass: "
                                            f"{root} taken (others: {others})")
            return root
        if len(tex) == 1:
            self.result.problems.append(f"no \\documentclass: {tex[0]}, the only .tex file, taken")
            return tex[0]
        with_begin = [rel for rel in tex if info[rel][1]]
        pool = with_begin or [rel for rel in tex if rel not in included] or tex
        root = max(pool, key=lambda rel: (self.f.paths[rel].stat().st_size, rel))
        self.result.problems.append(f"no \\documentclass: {root} guessed as the root")
        return root

    def run(self) -> Gathered:
        root = self.choose_root()
        if root is None:
            self.result.status = NO_TEX
            return self.result
        self.result.root = root
        self.result.text = self.expand(root, (root,))
        self.result.has_thebibliography = "\\begin{thebibliography}" in self.result.text.replace(" ", "")
        self.result.unused = [rel for rel in self.f.tex if rel not in self.result.files]
        declared = self.result.bib_files or self.result.embedded_bib
        if (self.result.bib_names_missing or self.result.bbl_input) and not self.result.has_thebibliography \
                and not declared:
            self.result.problems.append("references only in a .bbl file, which Milestone 1 did not keep")
        if not (self.result.has_thebibliography or declared or self.result.bib_names_missing
                or self.result.bbl_input):
            self.result.bib_files = [rel for rel in self.f.paths if rel.lower().endswith(".bib")]
            if self.result.bib_files:
                self.result.bib_fallback = True
                self.result.problems.append("no bibliography declared: the .bib files of the folder used as a fallback")
        self.result.problems = self.f.problems + self.result.problems
        return self.result


def gather(folder: Path, status: str = "success") -> Gathered:
    """Gather one version folder; `status` is its Milestone 1 status
    (versions.json). Anything but "success", or no folder, gives an empty
    document with status "no_source"."""
    folder = Path(folder)
    if status != "success" or not folder.is_dir():
        return Gathered(NO_SOURCE, problems=[f"Milestone 1 status: {status}"] if status != "success" else
                        ["version folder missing"])
    return _Gatherer(_Folder(folder)).run()


# ---------------------------------------------------------------------------
# Survey of a whole corpus (no network): how the rules above meet real papers
# ---------------------------------------------------------------------------

SURVEY_FIELDS = ["arxiv_id", "version", "status", "root", "files", "unused", "missing",
                 "bib_files", "bib_names_missing", "has_thebibliography", "chars", "seconds", "problems"]


def _version_statuses(paper_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((paper_dir / "versions.json").read_text(encoding="utf-8"))
        return {v: (r or {}).get("status", "") for v, r in data.get("versions", {}).items()}
    except (OSError, ValueError):
        return {}


def _survey_paper(paper_dir: str) -> list[dict]:
    paper_dir = Path(paper_dir)
    rows = []
    statuses = _version_statuses(paper_dir)
    for version in sorted((v for v in statuses if re.fullmatch(r"v[1-9]\d*", v)), key=lambda v: int(v[1:])):
        started = time.perf_counter()
        g = gather(paper_dir / version, statuses[version])
        rows.append({"arxiv_id": paper_dir.name, "version": version, "status": g.status,
                     "root": g.root or "", "files": len(g.files), "unused": len(g.unused),
                     "missing": len(g.missing), "bib_files": len(g.bib_files),
                     "bib_names_missing": len(g.bib_names_missing),
                     "has_thebibliography": g.has_thebibliography, "chars": len(g.text),
                     "seconds": round(time.perf_counter() - started, 4),
                     "problems": " | ".join(g.problems)[:2000]})
    return rows


def survey(data_dir: Path, out: Path, workers: int | None = None, limit: int | None = None) -> Counter:
    """Gather every version of data_dir/corpus, write one row per version to
    `out` and return the counts printed by main()."""
    papers = sorted(str(p) for p in (Path(data_dir) / "corpus").iterdir() if p.is_dir())[:limit]
    counts: Counter = Counter()
    started = time.perf_counter()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f, \
            ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        writer = csv.DictWriter(f, fieldnames=SURVEY_FIELDS)
        writer.writeheader()
        for rows in pool.map(_survey_paper, papers, chunksize=16):
            for row in rows:
                writer.writerow(row)
                counts["versions"] += 1
                counts[f"status_{row['status']}"] += 1
                counts["chars"] += row["chars"]
                for key, test in (("several_roots", "files with \\documentclass"),
                                  ("guessed_root", "no \\documentclass"),
                                  ("cycle", "cycle"), ("case_differs", "case differs"),
                                  ("bbl_only", "only in a .bbl"), ("not_utf8", "not UTF-8"),
                                  ("iffalse_unclosed", "\\iffalse without")):
                    counts[key] += test in row["problems"]
                counts["with_missing_inputs"] += row["missing"] > 0
                counts["with_unused_tex"] += row["unused"] > 0
                counts["multi_file"] += row["files"] > 1
    counts["seconds"] = round(time.perf_counter() - started, 1)
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Gather the LaTeX of one version folder, or survey a corpus")
    parser.add_argument("folder", nargs="?", type=Path, help="a version folder, e.g. data/corpus/2604-05535/v1")
    parser.add_argument("--data-dir", type=Path, help="with --survey: the Milestone 1 data folder")
    parser.add_argument("--survey", action="store_true", help="gather every version of --data-dir")
    parser.add_argument("--out", type=Path, help="survey CSV (default: <data-dir>/logs/gather_survey.csv)")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None, help="survey only the first N papers")
    args = parser.parse_args(argv)
    if args.survey:
        if not args.data_dir:
            parser.error("--survey needs --data-dir")
        out = args.out or args.data_dir / "logs" / "gather_survey.csv"
        counts = survey(args.data_dir, out, args.workers, args.limit)
        for key in sorted(counts):
            print(f"{key:>22}: {counts[key]}")
        print(f"Rows: {out}")
        return 0
    if not args.folder:
        parser.error("give a version folder, or --survey --data-dir DATA")
    g = gather(args.folder)
    print(json.dumps(g.summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
