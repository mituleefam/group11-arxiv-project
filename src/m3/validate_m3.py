"""Milestone 3 - check an exported Hugging Face dataset folder (export_hf.py) against the statements.

    python src/m3/validate_m3.py --dir HF_DIR [--streaming USER/DATASET] [--report report.json]

Checks, each printed as PASS / WARN / FAIL (exit code 1 when anything FAILs):
  layout      README.md with YAML `configs`, one per table, each pointing to data/<table>/*.parquet, and
              no other file type under data/
  schema      every file of a table has exactly the columns and types of the statements (export_hf.SCHEMAS):
              flat columns, one-level lists of strings only, dictionary-encoded kind/entry_type/partition
  non-null    the key columns hold no null
  unique keys papers.arxiv_id; elements.element_id; bibtex and predictions (arxiv_id, citation_key)
  links       hierarchy element_id and parent_id are elements; predictions rows are bibtex entries; every
              groundtruth and cand_* is a reference (with an arXiv ID) of the same paper; references, elements,
              bibtex and predictions papers are in papers (WARN: a paper may lack metadata)
  predictions partition in train/valid/test; cand_* distinct in a row and null only at the end; one row per
              bibtex entry of each predicted paper (WARN otherwise)
  files       shard sizes (WARN above 500 MB)
  --streaming loads every config of the published repository with datasets.load_dataset(..., streaming=True)
              and reads one row (needs `pip install datasets` and network access)
Only the tables present are checked, so a partial export can be checked too.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_hf import SCHEMAS, TABLES  # noqa: E402

MAX_FILE_MB = 500
KEY_COLUMNS = {t: [f.name for f in s if not f.nullable] for t, s in SCHEMAS.items()}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, status: str, check: str, detail: str = "") -> None:
        self.rows.append({"status": status, "check": check, "detail": detail})
        print(f"{status:4}  {check}" + (f": {detail}" if detail else ""), flush=True)

    def ok(self, cond: bool, check: str, detail: str = "", warn: bool = False) -> bool:
        self.add("PASS" if cond else ("WARN" if warn else "FAIL"), check, detail)
        return cond

    @property
    def failed(self) -> int:
        return sum(r["status"] == "FAIL" for r in self.rows)


def card_configs(readme: Path) -> dict[str, list[str]]:
    """config_name -> data_files paths, from the YAML front matter (a small parser: no PyYAML needed)."""
    text = readme.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    configs, current = {}, None
    for line in m.group(1).splitlines():
        name = re.match(r"\s*-\s*config_name:\s*(\S+)", line)
        path = re.match(r"\s*path:\s*(\S+)", line)
        if name:
            current = name.group(1)
            configs[current] = []
        elif path and current:
            configs[current].append(path.group(1).strip("'\""))
    return configs


def read_column(files: list[Path], column: str) -> pa.ChunkedArray:
    return pa.chunked_array([c for f in files for c in pq.read_table(f, columns=[column]).column(column).chunks],
                            type=pq.read_schema(files[0]).field(column).type)


def read_columns(files: list[Path], columns: list[str]) -> pa.Table:
    return pa.concat_tables([pq.read_table(f, columns=columns) for f in files])


def joined(*cols) -> pa.ChunkedArray:
    """Row-wise 'a|b' keys of string columns (dictionary columns decoded first)."""
    cols = [c.cast(pa.string()) if pa.types.is_dictionary(c.type) else c for c in cols]
    return pc.binary_join_element_wise(*cols, "|")


def check_schema(rep: Report, name: str, files: list[Path]) -> bool:
    want, good = SCHEMAS[name], True
    for f in files:
        got = pq.read_schema(f)
        if got.names != want.names:
            good &= rep.ok(False, f"schema {name}", f"{f.name}: columns {got.names}, expected {want.names}")
            continue
        for g, w in zip(got, want):
            if g.type != w.type:
                good &= rep.ok(False, f"schema {name}.{g.name}", f"{f.name}: type {g.type}, expected {w.type}")
            if pa.types.is_struct(g.type) or pa.types.is_map(g.type) or (
                    pa.types.is_list(g.type) and not pa.types.is_string(g.type.value_type)):
                good &= rep.ok(False, f"flat columns {name}.{g.name}", f"nested type {g.type}")
    if good:
        rep.ok(True, f"schema {name}", f"{len(files)} file(s), columns {', '.join(want.names)}")
    return good


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, required=True, help="the dataset folder written by export_hf.py")
    p.add_argument("--streaming", metavar="USER/DATASET", help="also load the published repository by streaming")
    p.add_argument("--report", type=Path, help="write the checks as JSON")
    args = p.parse_args(argv)
    rep, root = Report(), args.dir

    # ---- layout
    readme = root / "README.md"
    if not rep.ok(readme.is_file(), "dataset card", str(readme)):
        return 1
    configs = card_configs(readme)
    present = [t for t in TABLES if (root / "data" / t).is_dir()]
    rep.ok(bool(present), "tables present", ", ".join(present) or "none under data/")
    for t in present:
        rep.ok(configs.get(t) == [f"data/{t}/*.parquet"], f"card config {t}",
               f"data_files {configs.get(t)}")
    extra = sorted(set(configs) - set(present))
    rep.ok(not extra, "card configs without data", ", ".join(extra))
    strange = [str(x.relative_to(root)) for x in (root / "data").rglob("*") if x.is_file() and x.suffix != ".parquet"]
    rep.ok(not strange, "only Parquet under data/", ", ".join(strange[:5]))

    files = {t: sorted((root / "data" / t).glob("*.parquet")) for t in present}
    good = {t: bool(files[t]) and check_schema(rep, t, files[t]) for t in present}
    for t in present:
        big = [f"{f.name} {f.stat().st_size / 2**20:.0f} MB" for f in files[t] if f.stat().st_size > MAX_FILE_MB * 2**20]
        rep.ok(not big, f"file sizes {t}", ", ".join(big) or
               f"largest {max(f.stat().st_size for f in files[t]) / 2**20:.1f} MB", warn=True)
    ok = [t for t in present if good[t]]
    rows = {t: sum(pq.ParquetFile(f).metadata.num_rows for f in files[t]) for t in ok}
    for t in ok:
        print(f"      {t}: {rows[t]:,} rows")

    # ---- non-null keys
    for t in ok:
        for c in KEY_COLUMNS[t]:
            nulls = sum(pq.read_table(f, columns=[c]).column(c).null_count for f in files[t])
            rep.ok(nulls == 0, f"non-null {t}.{c}", f"{nulls} nulls")

    # ---- unique keys and links
    papers = read_column(files["papers"], "arxiv_id") if "papers" in ok else None
    if papers is not None:
        rep.ok(pc.count_distinct(papers).as_py() == len(papers), "unique papers.arxiv_id",
               f"{len(papers) - pc.count_distinct(papers).as_py()} duplicates")

    def in_papers(t: str):
        if papers is None or t not in ok:
            return
        ids = pc.unique(read_column(files[t], "arxiv_id"))
        missing = pc.sum(pc.invert(pc.is_in(ids, value_set=papers))).as_py() or 0
        rep.ok(missing == 0, f"{t} papers in papers", f"{missing} of {len(ids)} papers have no papers row", warn=True)

    refs_key = None
    if "references" in ok:
        r = read_columns(files["references"], ["arxiv_id", "ref_arxiv_id"])
        refs_key = joined(r.column("arxiv_id"), r.column("ref_arxiv_id"))
        dup = len(refs_key) - pc.count_distinct(refs_key).as_py()
        rep.ok(dup == 0, "unique references (arxiv_id, ref_arxiv_id)",
               f"{dup} repeated pairs (one row per crawled reference)", warn=True)
        in_papers("references")

    element_ids = None
    if "elements" in ok:
        element_ids = read_column(files["elements"], "element_id")
        dup = len(element_ids) - pc.count_distinct(element_ids).as_py()
        rep.ok(dup == 0, "unique elements.element_id", f"{dup} duplicates")
        in_papers("elements")
    if "hierarchy" in ok and element_ids is not None:
        h = read_columns(files["hierarchy"], ["element_id", "parent_id"])
        for c in ("element_id", "parent_id"):
            missing = pc.sum(pc.invert(pc.is_in(h.column(c), value_set=element_ids))).as_py() or 0
            rep.ok(missing == 0, f"hierarchy.{c} in elements", f"{missing} of {len(h)} edges")

    bib_key = None
    if "bibtex" in ok:
        b = read_columns(files["bibtex"], ["arxiv_id", "citation_key"])
        bib_key = joined(b.column("arxiv_id"), b.column("citation_key"))
        dup = len(bib_key) - pc.count_distinct(bib_key).as_py()
        rep.ok(dup == 0, "unique bibtex (arxiv_id, citation_key)", f"{dup} duplicates")
        in_papers("bibtex")

    if "predictions" in ok:
        cols = ["arxiv_id", "partition", "citation_key", "groundtruth_arxiv_id"] + [f"cand_{k}" for k in range(1, 6)]
        pr = read_columns(files["predictions"], cols)
        key = joined(pr.column("arxiv_id"), pr.column("citation_key"))
        dup = len(key) - pc.count_distinct(key).as_py()
        rep.ok(dup == 0, "unique predictions (arxiv_id, citation_key)", f"{dup} duplicates")
        parts = set(pc.unique(pr.column("partition").cast(pa.string())).to_pylist())
        rep.ok(parts <= {"train", "valid", "test"}, "predictions.partition values", ", ".join(sorted(map(str, parts))))
        cands = [pr.column(f"cand_{k}").to_pylist() for k in range(1, 6)]
        gaps = repeats = 0
        for row in zip(*cands):
            filled = [c for c in row if c is not None]
            gaps += row[:len(filled)] != tuple(filled)
            repeats += len(set(filled)) != len(filled)
        rep.ok(gaps == 0, "predictions cand_* null only at the end", f"{gaps} rows")
        rep.ok(repeats == 0, "predictions cand_* distinct", f"{repeats} rows")
        with_gt = pc.sum(pc.is_valid(pr.column("groundtruth_arxiv_id"))).as_py() or 0
        print(f"      predictions: {len(pr):,} rows, {with_gt:,} with a groundtruth "
              f"({len(pc.unique(pr.column('arxiv_id'))):,} papers)")
        if bib_key is not None:
            missing = pc.sum(pc.invert(pc.is_in(key, value_set=bib_key))).as_py() or 0
            rep.ok(missing == 0, "predictions entries are bibtex entries", f"{missing} rows")
            b_ids = b.column("arxiv_id")
            pred_papers = pc.unique(pr.column("arxiv_id"))
            in_pred = pc.is_in(b_ids, value_set=pred_papers)
            n_bib = pc.sum(in_pred).as_py() or 0
            rep.ok(n_bib == len(pr), "one predictions row per bibtex entry of each predicted paper",
                   f"{len(pr):,} rows for {n_bib:,} bibtex entries of those papers", warn=True)
        if refs_key is not None:
            for c in ["groundtruth_arxiv_id"] + [f"cand_{k}" for k in range(1, 6)]:
                col = pr.column(c)
                valid = pc.is_valid(col)
                k = joined(pr.column("arxiv_id").filter(valid), col.filter(valid))
                missing = pc.sum(pc.invert(pc.is_in(k, value_set=refs_key))).as_py() or 0
                rep.ok(missing == 0, f"predictions.{c} is a reference of the same paper", f"{missing} of {len(k):,}")
        in_papers("predictions")

    # ---- streaming from the Hub
    if args.streaming:
        try:
            from datasets import load_dataset
        except ImportError:
            rep.add("FAIL", "streaming", "pip install datasets")
        else:
            for t in present:
                try:
                    first = next(iter(load_dataset(args.streaming, t, split="train", streaming=True)))
                    rep.ok(sorted(first) == sorted(SCHEMAS[t].names), f"streaming {t}", f"first row keys {sorted(first)}")
                except Exception as exc:  # noqa: BLE001 - every failure of the Hub is a failed check
                    rep.add("FAIL", f"streaming {t}", f"{type(exc).__name__}: {exc}")

    print(f"\n{sum(r['status'] == 'PASS' for r in rep.rows)} PASS, {sum(r['status'] == 'WARN' for r in rep.rows)} WARN, "
          f"{rep.failed} FAIL")
    if args.report:
        args.report.write_text(json.dumps(rep.rows, indent=1), encoding="utf-8")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
