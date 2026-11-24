"""Milestone 3 - write the six tables of the final Hugging Face dataset as sharded Parquet, with its dataset card.

    python src/m3/export_hf.py --out HF_DIR
        [--m1-data-dir DIR [DIR ...] | --m1-snapshots BACKUP_DIR [BACKUP_DIR ...]]   papers, references
        [--m2-tables TABLES_DIR]                                                     elements, hierarchy, bibtex
        [--predictions WORK/predictions.parquet]                                     predictions
        [--range START_MONTH START_ID END_MONTH END_ID | --start ID --end ID]
        [--tables papers,references,...] [--shard-mb 200] [--drop-known-errors]
        [--title "..."] [--code-url URL]

Output (the layout of the statement):
    HF_DIR/README.md                         dataset card: YAML `configs`, one per table, and the description
    HF_DIR/data/<table>/<table>-NNNNN-of-MMMMM.parquet
    HF_DIR.manifest.json                     next to HF_DIR, not inside it: row counts, inputs, range, code commit

Sources
  papers       metadata.json of every paper folder of Milestone 1: arxiv_id, title, authors, submission_date,
               revised_dates, venue (the columns of the Milestone 1 statement).
  references   references_all.json of every paper folder: one row per crawled reference that has an arXiv ID
               ("keeping the arXiv-keyed references only"): arxiv_id (the citing paper), ref_arxiv_id, ref_title,
               ref_authors, ref_submission_date, ref_s2_id. The same reader as m3_data.py, so the references
               published are the ones the matching ranked.
  Both come from restored Milestone 1 data folders (--m1-data-dir, the --data-dir of main.py), or straight from
  the snapshot archives of snapshot.py (--m1-snapshots, a folder of NNNN_*.tar.gz): the archives are replayed in
  order, a later archive replacing a file and the "removed" list of an archive deleting it, exactly as
  snapshot.py restore does, but only metadata.json and references_all.json are read (no .tex is extracted).
  elements, hierarchy, bibtex
               the merged tables of pipeline_m2.py (--m2-tables: a folder holding elements/, hierarchy/, bibtex/),
               copied row group by row group with the column types of the Milestone 2 statement.
  predictions  predictions.parquet of src/m3/modeling/predict.py; its extra `range` column is dropped, so the
               columns are exactly those of the Milestone 3 statement.

--range / --start --end keep only the papers of one ID range, inclusive, in the same notation as main.py; the
filter applies to `arxiv_id` (the publication) in every table, so each member can publish the data of their own
range. Without it, everything given is written. --tables limits the export to some tables (default: every table
whose input is given).

Column types follow the statements: lists are one-level lists of strings (authors, revised_dates, ref_authors);
kind, entry_type and partition are dictionary-encoded strings; hierarchy.version and bibtex.year are integers;
the key columns are non-null. Files are cut at about --shard-mb megabytes of Arrow data each (row groups of at
most 100,000 rows), so the Hub viewer and streaming read them directly. No loading script is written.

--drop-known-errors removes the rows of KNOWN_M2_ERRORS from bibtex (a .bbl printed inside a Verbatim box, read as
a reference by M2). By default the frozen Milestone 2 table is published as it is and the dataset card lists the
known defects.

Nothing is uploaded: publishing is done by each member with their own token, for example
    huggingface-cli upload <user>/<dataset> HF_DIR . --repo-type dataset
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
from discovery import RangeBound  # noqa: E402  (Milestone 1: the range notation of main.py)
from snapshot import META, archives  # noqa: E402  (Milestone 1: the snapshot archives)

TEXT = pa.string()
LIST = pa.list_(pa.string())
DICT = pa.dictionary(pa.int32(), pa.string())  # as pipeline_m2.py writes kind and entry_type

SCHEMAS = {
    "papers": pa.schema([pa.field("arxiv_id", TEXT, nullable=False), pa.field("title", TEXT),
                         pa.field("authors", LIST), pa.field("submission_date", TEXT),
                         pa.field("revised_dates", LIST), pa.field("venue", TEXT)]),
    "references": pa.schema([pa.field("arxiv_id", TEXT, nullable=False), pa.field("ref_arxiv_id", TEXT, nullable=False),
                             pa.field("ref_title", TEXT), pa.field("ref_authors", LIST),
                             pa.field("ref_submission_date", TEXT), pa.field("ref_s2_id", TEXT)]),
    "elements": pa.schema([pa.field("element_id", TEXT, nullable=False), pa.field("arxiv_id", TEXT, nullable=False),
                           pa.field("kind", DICT), pa.field("content", TEXT)]),
    "hierarchy": pa.schema([pa.field("arxiv_id", TEXT, nullable=False), pa.field("version", pa.int32()),
                            pa.field("element_id", TEXT, nullable=False), pa.field("parent_id", TEXT, nullable=False)]),
    "bibtex": pa.schema([pa.field("arxiv_id", TEXT, nullable=False), pa.field("citation_key", TEXT, nullable=False),
                         pa.field("entry_type", DICT), pa.field("title", TEXT), pa.field("authors", LIST),
                         pa.field("year", pa.int32()), pa.field("doi", TEXT), pa.field("journal", TEXT),
                         pa.field("url", TEXT)]),
    "predictions": pa.schema([pa.field("arxiv_id", TEXT, nullable=False), pa.field("partition", DICT),
                              pa.field("citation_key", TEXT, nullable=False), pa.field("groundtruth_arxiv_id", TEXT)]
                             + [pa.field(f"cand_{k}", TEXT) for k in range(1, 6)]),
}
TABLES = list(SCHEMAS)
DESCRIPTIONS = {
    "papers": "the crawled metadata of each paper (Milestone 1)",
    "references": "one row per crawled reference with an arXiv ID and its citing paper (Milestone 1)",
    "elements": "the parsed elements of all versions, deduplicated into one store per paper (Milestone 2)",
    "hierarchy": "the node-parent edges of each version (Milestone 2)",
    "bibtex": "the unified BibTeX entries after deduplication (Milestone 2)",
    "predictions": "the reference-matching results: the top-5 candidate references of each BibTeX entry (Milestone 3)",
}
# rows of the frozen M2 bibtex table known to be wrong (see the dataset card)
KNOWN_M2_ERRORS = {("2605.16616", "aitchison1975statistical")}
ROW_GROUP = 100_000


# ---------------------------------------------------------------------------
# ID range
# ---------------------------------------------------------------------------

class IdRange:
    """An inclusive range of new-style arXiv IDs, compared as (yymm, number), never as strings."""

    def __init__(self, start: RangeBound, end: RangeBound):
        self.low, self.high = (int(start.yymm), start.number), (int(end.yymm), end.number)
        if self.high < self.low:
            raise ValueError("the end of the range comes before its start")
        self.text = f"{start.as_id()} .. {end.as_id()}"

    def __contains__(self, arxiv_id) -> bool:
        if not isinstance(arxiv_id, str) or "." not in arxiv_id or "/" in arxiv_id:
            return False  # an old-style ID (hep-th/9901001) is never part of an assigned range
        yymm, number = arxiv_id.split(".", 1)
        if not (yymm.isdigit() and number.split("v")[0].isdigit()):
            return False
        return self.low <= (int(yymm), int(number.split("v")[0])) <= self.high


def keep_mask(ids: pa.ChunkedArray | pa.Array, id_range: IdRange | None):
    """Boolean mask of the rows whose arxiv_id is in the range (all True without a range)."""
    if id_range is None:
        return None
    uniques = pc.unique(ids).to_pylist()
    inside = pa.array([u for u in uniques if u in id_range], type=pa.string())
    return pc.is_in(ids, value_set=inside)


# ---------------------------------------------------------------------------
# Milestone 1: papers and references
# ---------------------------------------------------------------------------

def paper_row(data: dict) -> dict:
    return {"arxiv_id": data.get("arxiv_id"), "title": data.get("title"),
            "authors": [str(a) for a in data.get("authors") or []],
            "submission_date": data.get("submission_date"),
            "revised_dates": [str(d) for d in data.get("revised_dates") or []],
            "venue": data.get("venue")}


def reference_rows(data: dict) -> list[dict]:
    """The arXiv-keyed references of one references_all.json, as m3_data.read_references reads them."""
    pid = data.get("arxiv_id")
    return [{"arxiv_id": pid, "ref_arxiv_id": r["ref_arxiv_id"], "ref_title": r.get("ref_title"),
             "ref_authors": [str(a) for a in r.get("ref_authors") or []],
             "ref_submission_date": r.get("ref_submission_date"), "ref_s2_id": r.get("ref_s2_id")}
            for r in data.get("references") or [] if r.get("ref_arxiv_id")]


WANTED = ("metadata.json", "references_all.json")


def _wanted(name: str) -> bool:
    p = PurePosixPath(name)
    return len(p.parts) == 3 and p.parts[0] == "corpus" and p.name in WANTED


def files_from_data_dir(data_dir: Path):
    """(relative path, bytes) of every metadata.json / references_all.json of a restored data folder."""
    corpus = data_dir / "corpus"
    if not corpus.is_dir():
        raise FileNotFoundError(f"{data_dir} has no corpus/ folder (give the --data-dir of main.py)")
    for folder in sorted(p for p in corpus.iterdir() if p.is_dir()):
        for name in WANTED:
            path = folder / name
            if path.is_file():
                yield f"corpus/{folder.name}/{name}", path.read_bytes()


def files_from_snapshots(backup_dir: Path):
    """The same files, read from the archives of snapshot.py without restoring them.

    Pass 1 replays the archives' member lists and "removed" lists to find, for every wanted file, the archive
    that holds its final version (or that it was deleted); pass 2 reads each file from that archive only."""
    found = archives(backup_dir)
    if not found:
        raise FileNotFoundError(f"no snapshot archive (NNNN_*.tar.gz) in {backup_dir}")
    final: dict[str, int] = {}  # rel path -> archive number holding its last version
    for seq, path in found:
        with tarfile.open(path, "r:gz") as tar:
            meta, names = {}, []
            for member in tar:
                if member.name == META:
                    meta = json.loads(tar.extractfile(member).read().decode("utf-8"))
                elif member.isfile() and _wanted(member.name):
                    names.append(member.name)
        if meta.get("full"):
            final = {}
        for rel in meta.get("removed", []):
            final.pop(rel, None)
        for rel in names:
            final[rel] = seq
    by_archive: dict[int, set[str]] = {}
    for rel, seq in final.items():
        by_archive.setdefault(seq, set()).add(rel)
    for seq, path in found:
        todo = by_archive.get(seq)
        if not todo:
            continue
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                if member.name in todo:
                    yield member.name, tar.extractfile(member).read()


def read_m1(sources, from_snapshots: bool, id_range: IdRange | None) -> tuple[list[dict], list[dict], dict]:
    papers, refs, stats = {}, {}, {"paper_folders": 0, "out_of_range": 0}
    for src in map(Path, sources):
        files = files_from_snapshots(src) if from_snapshots else files_from_data_dir(src)
        for rel, raw in files:
            data = json.loads(raw.decode("utf-8"))
            folder = PurePosixPath(rel).parts[1]
            pid = data.get("arxiv_id") or folder.replace("-", ".", 1)
            data["arxiv_id"] = pid
            if id_range is not None and pid not in id_range:
                stats["out_of_range"] += 1
                continue
            if rel.endswith("metadata.json"):
                papers[pid] = paper_row(data)
            else:
                refs[pid] = reference_rows(data)
    stats["paper_folders"] = len(set(papers) | set(refs))
    stats["papers_without_metadata"] = len(set(refs) - set(papers))
    paper_rows = [papers[p] for p in sorted(papers, key=_id_sort)]
    ref_rows = [r for p in sorted(refs, key=_id_sort) for r in refs[p]]
    return paper_rows, ref_rows, stats


def _id_sort(arxiv_id: str):
    try:
        yymm, number = arxiv_id.split(".", 1)
        return (0, int(yymm), int(number))
    except ValueError:
        return (1, 0, 0, arxiv_id)


# ---------------------------------------------------------------------------
# Writing sharded Parquet
# ---------------------------------------------------------------------------

def conform(table: pa.Table, schema: pa.Schema) -> pa.Table:
    """`table` with exactly the columns of `schema`, in order, cast to its types (dictionary-encoded where asked).
    A missing column is an error, so a renamed column never goes out silently as nulls."""
    missing = [f.name for f in schema if f.name not in table.column_names]
    if missing:
        raise ValueError(f"input lacks the columns {missing} (has {table.column_names})")
    columns = []
    for field in schema:
        col = table.column(field.name)
        if pa.types.is_dictionary(field.type):
            if pa.types.is_dictionary(col.type):
                col = col.cast(pa.string())
            col = pc.dictionary_encode(col.cast(pa.string()))
            col = col.cast(field.type)
        elif col.type != field.type:
            col = col.cast(field.type)
        if not field.nullable and col.null_count:
            raise ValueError(f"column {field.name} holds {col.null_count} nulls but must be non-null")
        columns.append(col)
    return pa.Table.from_arrays(columns, schema=schema)


class ShardWriter:
    """Writes one table as data/<name>/<name>-NNNNN-of-MMMMM.parquet, a new file every `shard_bytes`."""

    def __init__(self, out: Path, name: str, schema: pa.Schema, shard_bytes: int):
        self.folder = out / "data" / name
        self.folder.mkdir(parents=True, exist_ok=True)
        for old in self.folder.glob("*.parquet"):
            old.unlink()  # a re-export never mixes old shards with new ones
        self.name, self.schema, self.shard_bytes = name, schema, shard_bytes
        self.writer, self.size, self.files, self.rows = None, 0, [], 0

    def write(self, table: pa.Table) -> None:
        for batch in table.to_batches(max_chunksize=ROW_GROUP):
            if batch.num_rows == 0:
                continue
            if self.writer is None or self.size >= self.shard_bytes:
                self._open()
            self.writer.write_table(pa.Table.from_batches([batch], schema=self.schema), row_group_size=ROW_GROUP)
            self.size += batch.nbytes
            self.rows += batch.num_rows

    def _open(self) -> None:
        if self.writer is not None:
            self.writer.close()
        path = self.folder / f"{self.name}-{len(self.files):05d}.tmp.parquet"
        self.writer = pq.ParquetWriter(path, self.schema, compression="zstd")
        self.files.append(path)
        self.size = 0

    def close(self) -> list[Path]:
        if self.writer is None:  # an empty table still gets one (empty) file, so the config resolves
            self._open()
        self.writer.close()
        final = []
        for i, path in enumerate(self.files):
            target = self.folder / f"{self.name}-{i:05d}-of-{len(self.files):05d}.parquet"
            path.replace(target)
            final.append(target)
        return final


def write_rows(out: Path, name: str, rows: list[dict], shard_bytes: int) -> dict:
    schema = SCHEMAS[name]
    plain = pa.schema([pa.field(f.name, pa.string() if pa.types.is_dictionary(f.type) else f.type) for f in schema])
    table = conform(pa.Table.from_pylist(rows, schema=plain), schema)
    writer = ShardWriter(out, name, schema, shard_bytes)
    writer.write(table)
    files = writer.close()
    return {"rows": writer.rows, "files": len(files)}


def copy_parquet(out: Path, name: str, sources: list[Path], id_range: IdRange | None, shard_bytes: int,
                 known: set | None = None, drop: bool = False) -> dict:
    """Copy a (large) Parquet table batch by batch, filtered to the range and conformed to the schema.
    `known`: (arxiv_id, citation_key) keys of known wrong rows, counted (and removed when `drop`)."""
    schema = SCHEMAS[name]
    writer = ShardWriter(out, name, schema, shard_bytes)
    read = found = 0
    for path in sources:
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=ROW_GROUP):
            table = pa.Table.from_batches([batch])
            read += table.num_rows
            mask = keep_mask(table.column("arxiv_id"), id_range)
            if mask is not None:
                table = table.filter(mask)
            if known and table.num_rows:  # rows of KNOWN_M2_ERRORS inside the range: counted, dropped on request
                keys = zip(table.column("arxiv_id").to_pylist(), table.column("citation_key").to_pylist())
                bad = pa.array([k in known for k in keys])
                found += pc.sum(bad).as_py() or 0
                if drop:
                    table = table.filter(pc.invert(bad))
            if table.num_rows:
                writer.write(conform(table, schema))
    files = writer.close()
    result = {"rows": writer.rows, "files": len(files), "rows_read": read}
    if known:
        result["known_errors_found"] = found
        result["known_errors_dropped"] = found if drop else 0
    return result


def parquet_files(path: Path) -> list[Path]:
    """The .parquet files of a table: a file, or a folder of files (searched recursively, in name order)."""
    if path.is_file():
        return [path]
    files = sorted(path.rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no .parquet file under {path}")
    return files


# ---------------------------------------------------------------------------
# Dataset card
# ---------------------------------------------------------------------------

def schema_lines(schema: pa.Schema) -> str:
    def kind(t):
        if pa.types.is_dictionary(t):
            return "string, dictionary"
        if pa.types.is_list(t):
            return "list of strings"
        return {"string": "string", "int32": "int"}.get(str(t), str(t))
    return ", ".join(f"`{f.name}` ({kind(f.type)}{'' if f.nullable else ', non-null'})" for f in schema)


def dataset_card(tables: list[str], manifest: dict, title: str, code_url: str | None) -> str:
    yaml = ["---", f"pretty_name: {json.dumps(title)}", "configs:"]
    for name in tables:
        yaml += [f"- config_name: {name}", "  data_files:", "  - split: train", f"    path: data/{name}/*.parquet"]
    yaml.append("---")
    rng = manifest["range"] or "every paper of the inputs"
    body = [
        f"# {title}",
        "",
        "Processed arXiv corpus of an Introduction to Data Science group project: the crawled papers and their "
        "reference metadata (Milestone 1), the parsed and standardised LaTeX content (Milestone 2), and the "
        "reference-matching predictions (Milestone 3).",
        "",
        f"- **ID range:** {rng}",
        f"- **Exported:** {manifest['exported_at']}, code commit `{manifest['code_commit'] or 'unknown'}`",
    ]
    if code_url:
        body.append(f"- **Code:** <{code_url}>")
    body += ["", "## Tables", "", "| Table | Rows | Content |", "| :-- | --: | :-- |"]
    for name in tables:
        body.append(f"| `{name}` | {manifest['tables'][name]['rows']:,} | {DESCRIPTIONS[name]} |")
    body += ["", "Load one table with `datasets.load_dataset(\"<this repository>\", \"<table>\", split=\"train\")`; "
             "add `streaming=True` to read it without downloading.", "", "## Columns", ""]
    for name in tables:
        body.append(f"- **{name}**: {schema_lines(SCHEMAS[name])}")
    body += ["", "## Notes", ""]
    if "references" in tables:
        body.append("- `references` keeps only the references that have an arXiv ID; `arxiv_id` is the citing paper.")
    if "predictions" in tables:
        body.append("- `predictions` has one row per BibTeX entry of the papers used for matching (train, valid, test). "
                    "`groundtruth_arxiv_id` is null when the entry has no matching reference; `cand_1` is the best "
                    "candidate, and the `cand_*` cells are null when the paper has fewer than 5 references with an "
                    "arXiv ID. Candidates always come from the references of the same paper.")
    if "bibtex" in tables:
        info = manifest["tables"]["bibtex"]
        if info.get("known_errors_found"):  # only when the wrong row lies in the exported range
            known = ("removed from this export" if info.get("known_errors_dropped")
                     else "kept, since the Milestone 2 table is published as it was frozen")
            body.append("- Known defect of `bibtex`: the row (`2605.16616`, `aitchison1975statistical`) comes from a "
                        f".bbl printed inside a Verbatim box of an appendix, not from a real reference ({known}).")
        body.append("- `bibtex` rows read from free-text `\\bibitem` entries carry heuristic field errors (for example "
                    "a year taken from page numbers).")
    if "elements" in tables:
        body.append("- Known defect of `elements`/`hierarchy`: in about 20 of the group's 25,000 papers, the reference "
                    "list is parsed into the tree (the bibliography environments `mcitethebibliography`, "
                    "`CSLReferences` and the command form `\\thebibliography` are not recognised); their `bibtex` "
                    "rows are correct.")
    body.append("- The texts are those of the arXiv submissions; each paper keeps the license chosen by its authors "
                "on arXiv.")
    return "\n".join(yaml + [""] + body) + "\n"


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(SRC), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, required=True, help="the dataset folder to write (README.md + data/)")
    m1 = p.add_mutually_exclusive_group()
    m1.add_argument("--m1-data-dir", type=Path, nargs="+", help="restored Milestone 1 data folders (--data-dir of main.py)")
    m1.add_argument("--m1-snapshots", type=Path, nargs="+", help="Milestone 1 snapshot folders (NNNN_*.tar.gz)")
    p.add_argument("--m2-tables", type=Path, help="merged Milestone 2 tables: a folder holding elements/, hierarchy/, bibtex/")
    p.add_argument("--predictions", type=Path, help="predictions.parquet of src/m3/modeling/predict.py")
    p.add_argument("--range", nargs=4, dest="sheet_range", metavar=("START_MONTH", "START_ID", "END_MONTH", "END_ID"),
                   help="keep one ID range, written as the four sheet columns, e.g. 2026-04 05535 2026-04 10534")
    p.add_argument("--start", help="first ID of the range, e.g. 2604.05535 (with --end)")
    p.add_argument("--end", help="last ID of the range, e.g. 2604.10534 (with --start)")
    p.add_argument("--tables", help=f"comma-separated subset of {','.join(TABLES)} (default: every table whose input is given)")
    p.add_argument("--shard-mb", type=float, default=200.0, help="approximate size of one Parquet file (default 200)")
    p.add_argument("--drop-known-errors", action="store_true", help="remove KNOWN_M2_ERRORS from bibtex")
    p.add_argument("--title", default="arXiv reference matching dataset", help="pretty name of the dataset card")
    p.add_argument("--code-url", help="link to the code repository, written in the dataset card")
    args = p.parse_args(argv)
    if args.sheet_range and (args.start or args.end):
        p.error("use either --range or --start/--end, not both")
    if bool(args.start) != bool(args.end):
        p.error("--start and --end go together")
    try:
        if args.sheet_range:
            sm, si, em, ei = args.sheet_range
            args.id_range = IdRange(RangeBound.parse(f"{sm}:{si}"), RangeBound.parse(f"{em}:{ei}"))
        elif args.start:
            args.id_range = IdRange(RangeBound.parse(args.start), RangeBound.parse(args.end))
        else:
            args.id_range = None
    except ValueError as exc:
        p.error(str(exc))
    given = {"papers": args.m1_data_dir or args.m1_snapshots, "references": args.m1_data_dir or args.m1_snapshots,
             "elements": args.m2_tables, "hierarchy": args.m2_tables, "bibtex": args.m2_tables,
             "predictions": args.predictions}
    if args.tables:
        args.tables = [t.strip() for t in args.tables.split(",") if t.strip()]
        unknown = [t for t in args.tables if t not in SCHEMAS]
        if unknown:
            p.error(f"unknown tables {unknown}; choose among {TABLES}")
        lacking = [t for t in args.tables if not given[t]]
        if lacking:
            p.error(f"no input given for {lacking}")
    else:
        args.tables = [t for t in TABLES if given[t]]
    if not args.tables:
        p.error("nothing to export: give --m1-data-dir/--m1-snapshots, --m2-tables and/or --predictions")
    if args.shard_mb <= 0:
        p.error("--shard-mb must be positive")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    out, shard = args.out, int(args.shard_mb * 1024 * 1024)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out.parent / f"{out.name}.manifest.json"  # outside the dataset folder: not uploaded
    rng = args.id_range.text if args.id_range else None
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    kept = {t: v for t, v in previous.get("tables", {}).items()
            if t not in args.tables and (out / "data" / t).is_dir()}  # tables of an earlier run into this folder
    if kept and previous.get("range") != rng:
        print(f"!! {out} already holds {', '.join(kept)} exported for the range {previous.get('range') or 'all'}, "
              f"not {rng or 'all'}: export into a new folder, or export those tables again", file=sys.stderr)
        return 2
    manifest = {"exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "code_commit": git_commit(),
                "range": rng, "tables": dict(kept), "inputs": dict(previous.get("inputs", {}))}
    print(f"exporting {', '.join(args.tables)} -> {out}" + (f" (range {args.id_range.text})" if args.id_range else ""),
          flush=True)

    if {"papers", "references"} & set(args.tables):
        sources = args.m1_snapshots or args.m1_data_dir
        papers, refs, stats = read_m1(sources, bool(args.m1_snapshots), args.id_range)
        manifest["inputs"]["m1"] = {"sources": [str(s) for s in sources],
                                    "kind": "snapshots" if args.m1_snapshots else "data folders", **stats}
        for name, rows in (("papers", papers), ("references", refs)):
            if name in args.tables:
                manifest["tables"][name] = write_rows(out, name, rows, shard)
                print(f"  {name}: {manifest['tables'][name]['rows']:,} rows", flush=True)
    for name in ("elements", "hierarchy", "bibtex"):
        if name in args.tables:
            folder = args.m2_tables / name if (args.m2_tables / name).exists() else args.m2_tables
            known = KNOWN_M2_ERRORS if name == "bibtex" else None
            manifest["tables"][name] = copy_parquet(out, name, parquet_files(folder), args.id_range, shard, known,
                                                    drop=args.drop_known_errors)
            manifest["inputs"][name] = str(folder)
            print(f"  {name}: {manifest['tables'][name]['rows']:,} of {manifest['tables'][name]['rows_read']:,} rows",
                  flush=True)
    if "predictions" in args.tables:
        manifest["tables"]["predictions"] = copy_parquet(out, "predictions", parquet_files(args.predictions),
                                                         args.id_range, shard)
        manifest["inputs"]["predictions"] = str(args.predictions)
        print(f"  predictions: {manifest['tables']['predictions']['rows']:,} rows", flush=True)

    every = [t for t in TABLES if t in manifest["tables"]]  # this run's tables and those kept from earlier runs
    (out / "README.md").write_text(dataset_card(every, manifest, args.title, args.code_url), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"-> {out / 'README.md'}, {manifest_path}")
    print(f"check it:  python src/m3/validate_m3.py --dir {out}")
    print(f"publish:   huggingface-cli upload <user>/<dataset> {out} . --repo-type dataset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
