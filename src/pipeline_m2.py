"""Member 1 - Milestone 2 pipeline: Milestone 1 corpus -> elements, hierarchy, bibtex.

Every paper, with all its versions together, goes through (m2_contract.py
says which member's function each step calls):
  1. gatherer.gather: the compilation path and the flattened text of each version;
  2. Member 3 on the text and .bib files of each version: references
     (bibitem_parser.parse, bibtex_converter.convert);
  3. Member 3 across the versions: one row per reference and the citation
     key renames (dedup_bibtex.dedup), so that 4 can rename \\cite keys before
     sentences are compared;
  4. Member 2 and 3 on each version's text: cleaner.clean, normalizer.normalize,
     citation_updater.update;
  5. hierarchy.build, which cuts the text and calls Member 2's
     leaf_extractor.extract_leaves on each paragraph and Member 4's registry
     for every node, so a node has its deduplicated element_id when it is made;
  6. the paper's rows are written to corpus/<yymm-id>/parsed/: elements.json,
     hierarchy.json, bibtex.json, cite_rename_map.json and gathering.json
     (per version: root, files, missing inputs, problems).
Then the rows of every paper go into three Parquet tables (export_tables):
tables/elements/, tables/hierarchy/, tables/bibtex/.

A version without LaTeX source (pdf_only, no_tex, not_found, failed) still
gets its root node ("Version n root") and nothing under it.

    python src/pipeline_m2.py --data-dir data                 # every paper, then the tables
    python src/pipeline_m2.py --data-dir data --test          # trial run on 5 papers
    python src/pipeline_m2.py --data-dir data --ids 2604.05535 2604.05536
    python src/pipeline_m2.py --data-dir data --export-only   # tables from what is parsed
    python src/pipeline_m2.py --data-dir A B C --out tables   # several ranges, one set of tables

Resuming: logs/parse_manifest.jsonl (checkpoint.ParseManifest) gets one line
per parsed paper, after its files are written. A paper parsed by the same code (a hash of the Milestone 2
modules) is skipped by the next run, so an interrupted run goes on where it
stopped, and changing a module re-parses everything (--keep skips papers
parsed by older code too, --force re-parses all). logs/parse_log.csv has one
row per paper and run (counts, problems, seconds) for Member 4.

Papers are parsed by --workers processes at once (all CPUs by default); the
data folder should be on a local disk (on Colab /content, not Google Drive,
which is slow with many small files).

One paper never stops the run: an error fails that paper; a paper still running
after --paper-timeout seconds is stopped and fails (Linux only, as on Colab); a
worker process that dies (out of memory) makes the papers it may have held run
again one at a time, so only the paper that kills a worker alone fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import m2_contract
from checkpoint import ParseManifest
from common import CsvLog, DataLayout, Manifest, folder_name, now_iso
from gatherer import gather, read_text
from hierarchy import build
from m2_contract import BIBTEX_FIELDS, ELEMENT_FIELDS

SRC = Path(__file__).resolve().parent
OWN_MODULES = ("latex_scan", "gatherer", "hierarchy", "m2_contract", "pipeline_m2")
KINDS = ("heading", "paragraph", "list", "item", "sentence", "formula", "figure")
PARSE_LOG_FIELDS = (["time", "arxiv_id", "status", "versions", "versions_with_tex", "elements",
                     "edges", "bibtex"] + list(KINDS)
                    + ["missing_inputs", "unused_tex", "problems", "chars", "seconds", "error"])
TEST_PAPERS = 5
PAPER_TIMEOUT = 600   # seconds for one paper; the largest take seconds, so this means something is stuck
SUSPECTS = 16         # papers a dead worker may have held (workers x chunk of 4): run again one at a time
MAX_BATCH_CHARS = 500_000_000  # a Parquet string column holds 2 GB at most per batch
VERSION = re.compile(r"v[1-9]\d*")
_components = None


def components() -> m2_contract.Components:
    """The members' functions, loaded once per process (--stand-ins reaches
    the worker processes through the environment)."""
    global _components
    if _components is None:
        _components = m2_contract.load(use_modules=os.environ.get("M2_STAND_INS") != "1")
    return _components


def code_version(comp: m2_contract.Components) -> str:
    """Hash of the source of every Milestone 2 module in use: a change in any
    of them makes the next run parse every paper again."""
    names = list(OWN_MODULES) + [module for _, module, _, _ in m2_contract.SLOTS
                                 if not any(s.startswith(module + ".") for s in comp.stand_ins)]
    digest = hashlib.sha1()
    for name in sorted(set(names)):
        path = SRC / f"{name}.py"
        if path.is_file():
            digest.update(name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:12]


def write_json(path: Path, data) -> None:
    """Compact JSON written atomically (temporary file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as f:  # a file name may hold any byte
        f.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))  # dumps: the C encoder
    os.replace(tmp, path)


def version_statuses(paper_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((paper_dir / "versions.json").read_text(encoding="utf-8"))
        return {v: (r or {}).get("status", "") for v, r in data.get("versions", {}).items()}
    except (OSError, ValueError):
        return {}


def _bibtex_row(arxiv_id: str, row: dict) -> dict:
    out = {name: row.get(name) for name in BIBTEX_FIELDS}
    out["arxiv_id"] = arxiv_id
    authors = out.get("authors")
    out["authors"] = [str(a) for a in authors] if isinstance(authors, (list, tuple)) else (
        [] if authors in (None, "") else [str(authors)])
    try:
        year = int(out["year"]) if out.get("year") not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    out["year"] = year if year is not None and 0 < year < 3000 else None  # an int column: never a wild number
    return out


class PaperTimeout(Exception):
    pass


def _stop_paper(signum, frame):
    raise PaperTimeout("stopped: still running after the time allowed for one paper")


def _set_alarm(seconds: float) -> None:
    """Raise PaperTimeout in this process after `seconds` (0: cancel). Needs SIGALRM (Linux)."""
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _stop_paper)
        signal.setitimer(signal.ITIMER_REAL, seconds)


def parse_paper(task: dict) -> dict:
    """Parse one paper (all versions) and write its parsed/ files; returns
    the paper's record for logs/parse_manifest.jsonl."""
    started = time.perf_counter()
    arxiv_id, paper_dir = task["arxiv_id"], Path(task["paper_dir"])
    record = {"arxiv_id": arxiv_id, "code": task["code"], "status": "failed"}
    try:
        _set_alarm(task.get("timeout", 0))
        comp = components()
        statuses = version_statuses(paper_dir)
        versions = [v for v in (task["versions"] or statuses) if VERSION.fullmatch(v)]
        gathered = {int(v[1:]): gather(paper_dir / v, statuses.get(v, "missing"))
                    for v in sorted(set(versions), key=lambda v: int(v[1:]))}

        by_version = {}
        for number, g in gathered.items():
            if g.ok:
                bib_files = {rel: read_text(paper_dir / f"v{number}" / rel)[0] for rel in g.bib_files}
                bib_files.update(g.embedded_bib)
                by_version[number] = comp.convert_references(comp.parse_references(g.text, bib_files))
        bib_rows, rename = comp.dedup_references(by_version) if by_version else ([], {})

        registry = comp.Registry(arxiv_id)
        edges, kinds, problems = [], Counter(), {}
        for number in sorted(gathered):
            root = registry.element_id("document", f"Version {number} root", None, 0)
            g = gathered[number]
            problems[number] = list(g.problems)
            if not g.ok:
                continue
            text = comp.update_citations(comp.normalize(comp.clean(g.text)), rename)
            tree = build(text, root, registry, comp.extract_leaves)
            edges.extend([number, child, parent] for child, parent in tree.edges)
            kinds.update(tree.kinds)
            problems[number] += tree.problems
        elements = registry.rows()
        bibtex = [_bibtex_row(arxiv_id, row) for row in bib_rows]

        parsed = paper_dir / "parsed"
        write_json(parsed / "elements.json", {"arxiv_id": arxiv_id, "columns": ELEMENT_FIELDS[0:1] + ELEMENT_FIELDS[2:],
                                              "rows": [[e["element_id"], e["kind"], e["content"]] for e in elements]})
        write_json(parsed / "hierarchy.json", {"arxiv_id": arxiv_id, "columns": ["version", "element_id", "parent_id"],
                                               "rows": edges})
        write_json(parsed / "bibtex.json", {"arxiv_id": arxiv_id, "rows": bibtex})
        write_json(parsed / "cite_rename_map.json", rename)
        write_json(parsed / "gathering.json", {f"v{n}": dict(g.summary(), problems=problems[n])
                                               for n, g in gathered.items()})
        record.update(status="done", versions=len(gathered),
                      versions_with_tex=sum(g.ok for g in gathered.values()), elements=len(elements),
                      edges=len(edges), bibtex=len(bibtex), **{k: kinds.get(k, 0) for k in KINDS},
                      missing_inputs=sum(len(g.missing) for g in gathered.values()),
                      unused_tex=sum(len(g.unused) for g in gathered.values()),
                      problems=sum(len(p) for p in problems.values()),
                      chars=sum(len(g.text) for g in gathered.values()))
    except Exception as exc:  # one paper never stops the run
        error = f"{type(exc).__name__}: {exc} | " + traceback.format_exc(limit=3).replace("\n", " ")[-600:]
        record["error"] = error.encode("utf-8", "backslashreplace").decode("utf-8")
    finally:
        _set_alarm(0)
    record["seconds"] = round(time.perf_counter() - started, 3)
    record["finished_at"] = now_iso()
    return record


def list_papers(layout: DataLayout, ids: list[str] | None = None) -> list[dict]:
    """Papers to parse: the Milestone 1 manifest's found papers (or, without
    a manifest, every folder of corpus/), with their version lists."""
    records = Manifest(layout.manifest).load()
    papers = []
    if records:
        for arxiv_id, r in records.items():
            if r.get("status") == "success":
                papers.append({"arxiv_id": arxiv_id, "paper_dir": str(layout.paper_dir(arxiv_id)),
                               "versions": list(r.get("versions") or [])})
        others = Counter(r.get("status") for r in records.values() if r.get("status") != "success")
        if others:
            print(f"  left out, not papers found by Milestone 1: {dict(others)} (Member 4: coverage)")
    elif layout.corpus.is_dir():
        for folder in layout.corpus.iterdir():
            if folder.is_dir():
                papers.append({"arxiv_id": folder.name.replace("-", ".", 1), "paper_dir": str(folder),
                               "versions": []})
    papers.sort(key=lambda p: tuple(int(x) if x.isdigit() else 0 for x in p["arxiv_id"].replace("/", ".").split(".")))
    if ids:
        wanted = set(ids)
        papers = [p for p in papers if p["arxiv_id"] in wanted or folder_name(p["arxiv_id"]) in wanted]
    return papers


def run(layout: DataLayout, papers: list[dict], *, workers: int, code: str,
        timeout: float = PAPER_TIMEOUT) -> Counter:
    """Parse `papers` with `workers` processes; returns the counts."""
    log = CsvLog(layout.logs / "parse_log.csv", PARSE_LOG_FIELDS)
    manifest = ParseManifest(layout.logs)
    totals: Counter = Counter()
    for paper in papers:
        paper["code"], paper["timeout"] = code, timeout
    started, shown = time.perf_counter(), 0

    def note(number: int, record: dict) -> None:
        nonlocal shown
        manifest.append(record)
        log.write(time=record["finished_at"], **{k: record.get(k, "") for k in PARSE_LOG_FIELDS if k != "time"})
        totals["papers"] += 1
        totals[record["status"]] += 1
        for key in ("versions", "versions_with_tex", "elements", "edges", "bibtex", "chars") + KINDS:
            totals[key] += record.get(key, 0) or 0
        if record["status"] != "done":
            print(f"[m2] {record['arxiv_id']} failed: {record.get('error', '')[:300]}", flush=True)
        now = time.perf_counter()
        if number == len(papers) or now - shown >= 30:
            shown = now
            rate = number / (now - started)
            print(f"[m2] {number}/{len(papers)} papers | {totals['versions']} versions | "
                  f"{totals['elements']} elements, {totals['edges']} edges | {rate:.1f} papers/s | "
                  f"about {(len(papers) - number) / rate / 60:.0f} min left", flush=True)

    position, suspects = 0, 0  # suspects: the next papers to run alone, after a worker died
    while position < len(papers):
        batch = papers[position:position + suspects] if suspects else papers[position:]
        pool = ProcessPoolExecutor(max_workers=1 if suspects else workers)
        finished = 0
        try:
            for record in pool.map(parse_paper, batch, chunksize=1 if suspects else 4):
                finished += 1
                note(position + finished, record)
            suspects = 0
            position += finished
        except BrokenProcessPool:
            position += finished
            if suspects:  # alone, so this paper killed its worker
                paper = papers[position]
                print(f"[m2] {paper['arxiv_id']}: its process died (out of memory?), the paper fails", flush=True)
                note(position + 1, {"arxiv_id": paper["arxiv_id"], "code": code, "status": "failed", "seconds": 0,
                                    "error": "worker process died (out of memory?)", "finished_at": now_iso()})
                position += 1
                suspects = max(suspects - finished - 1, 0)
            else:
                print(f"[m2] a worker process died: the next {SUSPECTS} papers run one at a time", flush=True)
                suspects = SUSPECTS
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    totals["seconds"] = round(time.perf_counter() - started, 1)
    return totals


# ---------------------------------------------------------------------------
# Parquet tables
# ---------------------------------------------------------------------------

def table_schemas():
    import pyarrow as pa
    text = pa.string()
    dictionary = pa.dictionary(pa.int32(), pa.string())
    return {
        "elements": pa.schema([pa.field("element_id", text, nullable=False),
                               pa.field("arxiv_id", text, nullable=False),
                               pa.field("kind", dictionary), pa.field("content", text)]),
        "hierarchy": pa.schema([pa.field("arxiv_id", text, nullable=False), pa.field("version", pa.int32()),
                                pa.field("element_id", text, nullable=False),
                                pa.field("parent_id", text, nullable=False)]),
        "bibtex": pa.schema([pa.field("arxiv_id", text, nullable=False),
                             pa.field("citation_key", text, nullable=False),
                             pa.field("entry_type", dictionary), pa.field("title", text),
                             pa.field("authors", pa.list_(text)), pa.field("year", pa.int32()),
                             pa.field("doi", text), pa.field("journal", text), pa.field("url", text)]),
    }


class _TableWriter:
    """Rows of one table, written in row groups of `batch` rows."""

    def __init__(self, folder: Path, schema, batch: int = 200_000):
        import pyarrow.parquet as pq
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("part-*.parquet"):
            old.unlink()
        self.schema, self.batch = schema, batch
        self.path = folder / "part-00000.parquet"
        self.writer = pq.ParquetWriter(self.path, schema, compression="zstd")
        self.columns = {name: [] for name in schema.names}
        self.rows = 0
        self.chars = 0

    def add(self, *values) -> None:
        for column, value in zip(self.columns.values(), values):
            column.append(value)
            if isinstance(value, str):
                self.chars += len(value)
        if self.chars >= MAX_BATCH_CHARS or len(next(iter(self.columns.values()))) >= self.batch:
            self.flush()

    def flush(self) -> None:
        import pyarrow as pa
        count = len(next(iter(self.columns.values())))
        if not count:
            return
        arrays = []
        for field in self.schema:
            values = self.columns[field.name]
            if pa.types.is_dictionary(field.type):
                arrays.append(pa.array(values, type=pa.string()).dictionary_encode())
            else:
                arrays.append(pa.array(values, type=field.type))
        self.writer.write_table(pa.Table.from_arrays(arrays, schema=self.schema))
        self.rows += count
        self.chars = 0
        for column in self.columns.values():
            column.clear()

    def close(self) -> int:
        self.flush()
        self.writer.close()
        return self.rows


def export_tables(done: dict[str, dict], papers: list[dict], out: Path) -> dict:
    """The three tables from the parsed/ files of `papers`, in paper order
    (papers whose last parse failed, in `done`, are left out and counted)."""
    schemas = table_schemas()
    writers = {name: _TableWriter(out / name, schema) for name, schema in schemas.items()}
    exported = Counter()
    for paper in papers:
        if done.get(paper["arxiv_id"], {}).get("status") != "done":
            exported["papers_not_parsed"] += 1
            continue
        parsed = Path(paper["paper_dir"]) / "parsed"
        try:
            elements = json.loads((parsed / "elements.json").read_text(encoding="utf-8"))
            edges = json.loads((parsed / "hierarchy.json").read_text(encoding="utf-8"))
            bibtex = json.loads((parsed / "bibtex.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            exported["papers_without_parsed_files"] += 1
            continue
        arxiv_id = elements["arxiv_id"]
        for element_id, kind, content in elements["rows"]:
            writers["elements"].add(element_id, arxiv_id, kind, content)
        for version, element_id, parent_id in edges["rows"]:
            writers["hierarchy"].add(arxiv_id, version, element_id, parent_id)
        for row in bibtex["rows"]:
            if not row.get("citation_key"):  # non-null column: a row without a key is counted, not written
                exported["bibtex_rows_without_key"] += 1
                continue
            writers["bibtex"].add(*(row.get(name) for name in BIBTEX_FIELDS))
        exported["papers"] += 1
    for name, writer in writers.items():
        exported[f"{name}_rows"] = writer.close()
        exported[f"{name}_bytes"] = writer.path.stat().st_size
    write_json(out / "tables.json", dict(exported, exported_at=now_iso()))
    return dict(exported)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Milestone 2: parse the corpus into elements, hierarchy, bibtex")
    parser.add_argument("--data-dir", type=Path, nargs="+", default=[Path("data")],
                        help="Milestone 1 data folder(s), one per range")
    parser.add_argument("--out", type=Path, help="folder of the Parquet tables (default: <data-dir>/tables; "
                                                 "needed with several data folders)")
    parser.add_argument("--ids", nargs="+", help="only these papers (2604.05535 or 2604-05535)")
    parser.add_argument("--all", action="store_true", help="every paper (the default)")
    parser.add_argument("--test", action="store_true", help=f"trial run on the first {TEST_PAPERS} papers")
    parser.add_argument("--limit", type=int, help="only the first N papers")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1, help="processes (default: all CPUs)")
    parser.add_argument("--paper-timeout", type=float, default=PAPER_TIMEOUT,
                        help="seconds after which a paper is stopped and fails")
    parser.add_argument("--force", action="store_true", help="parse again papers already parsed")
    parser.add_argument("--keep", action="store_true", help="skip papers parsed by an older version of the code")
    parser.add_argument("--no-export", action="store_true", help="do not write the Parquet tables")
    parser.add_argument("--export-only", action="store_true", help="only write the tables from parsed/ files")
    parser.add_argument("--stand-ins", action="store_true",
                        help="use m2_contract's stand-ins even where a member's module exists")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    global _components
    args = parse_args(argv)
    if len(args.data_dir) > 1 and not args.out:
        print("With several data folders, say where the tables go: --out")
        return 2
    if args.stand_ins:
        _components = m2_contract.load(use_modules=False)
        os.environ["M2_STAND_INS"] = "1"
    comp = components()
    code = code_version(comp)
    for missing in comp.stand_ins:
        print(f"  stand-in for {missing} (m2_contract.py)")
    every_paper, done = [], {}
    for data_dir in args.data_dir:
        layout = DataLayout(data_dir)
        layout.logs.mkdir(parents=True, exist_ok=True)
        papers = list_papers(layout, args.ids)
        if args.test or args.limit:
            papers = papers[:TEST_PAPERS if args.test else args.limit]
        if not papers:
            print(f"No paper in {layout.manifest} or {layout.corpus}")
            return 2
        print(f"Milestone 2 pipeline | {len(papers)} papers | data: {layout.root} | code {code}")
        manifest = ParseManifest(layout.logs)
        if not args.export_only:
            parsed = manifest.load()
            todo = [p for p in papers if args.force
                    or not manifest.is_done(parsed.get(p["arxiv_id"]), code, keep=args.keep)]
            print(f"  {len(todo)} to parse, {len(papers) - len(todo)} already parsed by this code, "
                  f"{args.workers} processes")
            if todo:
                totals = run(layout, todo, workers=max(1, args.workers), code=code, timeout=args.paper_timeout)
                print("[m2] " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())))
        every_paper += papers
        done.update(manifest.load())
    papers = every_paper
    if not args.no_export:
        out = args.out or layout.root / "tables"
        try:
            exported = export_tables(done, papers, out)
        except ImportError:
            print("Tables not written: pyarrow is missing (pip install pyarrow)")
            return 1
        print("[tables] " + ", ".join(f"{k}={v}" for k, v in sorted(exported.items())) + f" | {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
