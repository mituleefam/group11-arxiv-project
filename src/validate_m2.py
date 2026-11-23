"""Member 4 (M2): validate parsed elements, edges, references and Parquet schema.

    python src/validate_m2.py --data-dir data --limit 5
    python src/validate_m2.py --data-dir data --require-parquet

For each version the hierarchy must form a tree: every child ID has one
parent, no duplicate edges or cycles, and every node is reachable from root.
Equivalent content may share an element ID ACROSS versions but not for
separate occurrences within the same version. A version without TeX
legitimately has a document root with zero edges.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from checkpoint import ParseManifest
from common import DataLayout
from pipeline_m2 import list_papers
from reference_parser.bibitem_parser import iter_cite_groups

COLUMNS = ("arxiv_id", "version", "check_name", "status", "details")
DOCUMENT_RE = re.compile(r"^Version (\d+) root$")
BIB_FIELDS = ("arxiv_id", "citation_key", "entry_type", "title", "authors", "year", "doi", "journal", "url")
VALID_KINDS = {"document", "heading", "paragraph", "list", "item", "sentence", "formula", "figure"}
LEAF_KINDS = {"sentence", "formula", "figure"}


def expected_arrow_schemas():
    """Independent schema contract, not inferred from pipeline output."""
    import pyarrow as pa
    s, d, i = pa.string(), pa.dictionary(pa.int32(), pa.string()), pa.int32()
    return {
        "elements": pa.schema([pa.field("element_id", s, nullable=False),
                               pa.field("arxiv_id", s, nullable=False),
                               pa.field("kind", d), pa.field("content", s)]),
        "hierarchy": pa.schema([pa.field("arxiv_id", s, nullable=False),
                                pa.field("version", i),
                                pa.field("element_id", s, nullable=False),
                                pa.field("parent_id", s, nullable=False)]),
        "bibtex": pa.schema([pa.field("arxiv_id", s, nullable=False),
                             pa.field("citation_key", s, nullable=False),
                             pa.field("entry_type", d), pa.field("title", s),
                             pa.field("authors", pa.list_(s)), pa.field("year", i),
                             pa.field("doi", s), pa.field("journal", s), pa.field("url", s)]),
    }


class Reporter:
    def __init__(self):
        self.rows = []

    def add(self, paper: str, version: str | int, name: str,
            status: str, details: str = "") -> None:
        self.rows.append(dict(zip(COLUMNS, (paper, str(version), name, status, str(details)))))

    def counts(self) -> dict[str, int]:
        return dict(Counter(row["status"] for row in self.rows))

    def write(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        with filename.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(self.rows)


def _load(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _verify_graph(paper: str, version: int, elements: dict, root: str,
                  edges: list[tuple[str, str]], reporter: Reporter) -> None:
    children = defaultdict(set)
    all_ids = {root}
    bad_parent = []
    bad_child = []
    parent_sets = defaultdict(set)
    for child, parent in edges:
        parent_sets[child].add(parent)
        if parent not in elements:
            bad_parent.append(parent)
        if child not in elements:
            bad_child.append(child)
        children[parent].add(child)
        all_ids.add(child)
        all_ids.add(parent)
    duplicate_edges = len(edges) - len(set(edges))
    multiparent = {child: parents for child, parents in parent_sets.items()
                   if len(parents) > 1}
    reporter.add(paper, version, "unique_parent_per_version",
                 "FAIL" if multiparent else "PASS",
                 f"nodes_with_multiple_parents={len(multiparent)}, examples={list(multiparent)[:5]}")
    reporter.add(paper, version, "duplicate_hierarchy_edges",
                 "FAIL" if duplicate_edges else "PASS",
                 f"duplicate_edges={duplicate_edges}")
    if bad_parent or bad_child:
        reporter.add(paper, version, "edge_elements_exist", "FAIL",
                     f"missing parents={len(bad_parent)}, children={len(bad_child)}")
        return
    reporter.add(paper, version, "edge_elements_exist", "PASS", f"edges={len(edges)}")
    if any(child == root for child, _ in edges):
        reporter.add(paper, version, "root_not_child", "FAIL", "root is a child")
    else:
        reporter.add(paper, version, "root_not_child", "PASS")
    # Cycle check uses an iterative DFS (avoids RecursionError for deep trees).
    color = {}
    cycles = []
    for start in all_ids:
        if color.get(start, 0):
            continue
        color[start] = 1
        stack = [(start, iter(children[start]))]
        while stack:
            node, iterator = stack[-1]
            nxt = next(iterator, None)
            if nxt is None:
                color[node] = 2
                stack.pop()
            elif color.get(nxt, 0) == 1:
                cycles.append((node, nxt))
            elif color.get(nxt, 0) == 0:
                color[nxt] = 1
                stack.append((nxt, iter(children[nxt])))
    reporter.add(paper, version, "cycle_free", "FAIL" if cycles else "PASS",
                 f"back_edges={len(cycles)}")
    reached, stack = {root}, [root]
    while stack:
        node = stack.pop()
        for child in children[node]:
            if child not in reached:
                reached.add(child)
                stack.append(child)
    unreachable = all_ids - reached
    reporter.add(paper, version, "root_reachability", "FAIL" if unreachable else "PASS",
                 f"unreachable={len(unreachable)}")
    wrong_leaves = [node for node in reached if elements[node]["kind"] in LEAF_KINDS and children[node]]
    reporter.add(paper, version, "leaf_has_no_children", "FAIL" if wrong_leaves else "PASS",
                 f"leaf_parents={len(wrong_leaves)}")
    unexpected_leaf_kinds = Counter(elements[n]["kind"] for n in reached
                                    if n not in children or not children[n]
                                    if elements[n]["kind"] not in LEAF_KINDS and n != root)
    if unexpected_leaf_kinds:
        reporter.add(paper, version, "nonstandard_leaf_kinds", "WARN", str(dict(unexpected_leaf_kinds)))


def validate_paper(paper: dict, reporter: Reporter) -> dict[str, int]:
    """Validate one paper's intermediate JSONs without requiring Parquet."""
    arxiv_id = paper["arxiv_id"]
    parsed = Path(paper["paper_dir"]) / "parsed"
    try:
        elem_doc, hier_doc, bib_doc, gathering = (
            _load(parsed / name) for name in
            ("elements.json", "hierarchy.json", "bibtex.json", "gathering.json"))
    except (OSError, ValueError, TypeError) as exc:
        reporter.add(arxiv_id, "", "parsed_files", "FAIL", repr(exc))
        return {}
    if (elem_doc.get("arxiv_id") != arxiv_id or hier_doc.get("arxiv_id") != arxiv_id
            or elem_doc.get("columns") != ["element_id", "kind", "content"]
            or hier_doc.get("columns") != ["version", "element_id", "parent_id"]):
        reporter.add(arxiv_id, "", "json_headers", "FAIL", "arxiv_id/columns mismatch")
    else:
        reporter.add(arxiv_id, "", "json_headers", "PASS")
    elements = {}
    invalid = 0
    id_prefix = arxiv_id.replace(".", "-").replace("/", "-") + "#"
    for row in elem_doc.get("rows", []):
        if (not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str)
                or not row[0].startswith(id_prefix) or not isinstance(row[1], str)
                or not isinstance(row[2], str) or row[0] in elements):
            invalid += 1
            continue
        elements[row[0]] = {"kind": row[1], "content": row[2]}
        if row[1] not in VALID_KINDS:
            invalid += 1
    reporter.add(arxiv_id, "", "elements_rows", "FAIL" if invalid else "PASS",
                 f"rows={len(elem_doc.get('rows', []))}, invalid_or_duplicate={invalid}")
    invalid_bib = 0
    keys = set()
    for row in bib_doc.get("rows", []):
        if not isinstance(row, dict):
            invalid_bib += 1
            continue
        key = row.get("citation_key")
        if (set(row) != set(BIB_FIELDS) or row.get("arxiv_id") != arxiv_id
                or not isinstance(key, str) or not key
                or key in keys or not isinstance(row.get("authors"), list)
                or any(not isinstance(a, str) for a in row["authors"])
                or any(row.get(field) is not None and not isinstance(row[field], str)
                       for field in ("entry_type", "title", "doi", "journal", "url"))
                or (row.get("year") is not None and
                    (type(row.get("year")) is not int or not 0 < row["year"] < 3000))):
            invalid_bib += 1
        else:
            keys.add(key)
    replacement_chars = sum("\ufffd" in row["content"] for row in elements.values())
    replacement_chars += sum(any("\ufffd" in str(v) for v in row.values())
                             for row in bib_doc.get("rows", []) if isinstance(row, dict))
    if replacement_chars:
        reporter.add(arxiv_id, "", "encoding_replacement_chars", "WARN",
                     f"records_containing_U+FFFD={replacement_chars}; inspect original source")
    reporter.add(arxiv_id, "", "bibtex_rows", "FAIL" if invalid_bib else "PASS",
                 f"rows={len(bib_doc.get('rows', []))}, invalid={invalid_bib}")
    all_edges = defaultdict(list)
    malformed_edges = 0
    for row in hier_doc.get("rows", []):
        if (not isinstance(row, list) or len(row) != 3 or type(row[0]) is not int
                or not isinstance(row[1], str) or not isinstance(row[2], str)):
            malformed_edges += 1
        else:
            all_edges[row[0]].append((row[1], row[2]))
    reporter.add(arxiv_id, "", "hierarchy_rows", "FAIL" if malformed_edges else "PASS",
                 f"edges={len(hier_doc.get('rows', []))}, malformed={malformed_edges}")
    expected_versions = set()
    for label in gathering:
        if re.fullmatch(r"v[1-9]\d*", label):
            expected_versions.add(int(label[1:]))
    if not expected_versions:
        reporter.add(arxiv_id, "", "versions_found", "FAIL", "gathering.json has no versions")
    actual_versions = set(all_edges)
    stray_versions = actual_versions - expected_versions
    reporter.add(arxiv_id, "", "version_coverage", "FAIL" if stray_versions else "PASS",
                 f"gathered={len(expected_versions)}, edges versions not gathered={sorted(stray_versions)}")
    roots = defaultdict(list)
    for element_id, item in elements.items():
        if item["kind"] == "document":
            m = DOCUMENT_RE.fullmatch(item["content"])
            if m:
                roots[int(m.group(1))].append(element_id)
            else:
                reporter.add(arxiv_id, "", "root_content", "FAIL", element_id)
    for version in sorted(expected_versions):
        found = roots.get(version, [])
        reporter.add(arxiv_id, version, "one_document_root", "PASS" if len(found) == 1 else "FAIL",
                     f"roots={len(found)}")
        if len(found) != 1:
            continue
        root = found[0]
        edges = all_edges.get(version, [])
        _verify_graph(arxiv_id, version, elements, root, edges, reporter)
        status = gathering[f"v{version}"].get("status")
        if status != "ok":
            reporter.add(arxiv_id, version, "no_tex_version", "PASS" if not edges else "FAIL",
                         f"gather_status={status}, edges={len(edges)}")
        elif not edges:
            reporter.add(arxiv_id, version, "empty_tex_tree", "WARN", "TeX version has no edges")
    extra_roots = set(roots) - expected_versions
    if extra_roots:
        reporter.add(arxiv_id, "", "unexpected_roots", "FAIL", str(sorted(extra_roots)))
    used_ids = {x for edges in all_edges.values() for edge in edges for x in edge}
    used_ids.update(r for ids in roots.values() for r in ids)
    unused_elements = set(elements) - used_ids
    reporter.add(arxiv_id, "", "unused_elements", "FAIL" if unused_elements else "PASS",
                 f"unused={len(unused_elements)}")
    # Cites are counted by unique (paper, key), not by textual occurrence.
    cited = set()
    for item in elements.values():
        text = item["content"]
        # Share the citation grammar with the reference parser, including
        # \\Cite, biblatex multicites and optional notes. A naked
        # \\nocite{*} requests all references; it is not a missing key.
        for begin, end in iter_cite_groups(text):
            cited.update(key.strip() for key in text[begin:end].split(",")
                         if key.strip() and key.strip() != "*")
    unmatched = sorted(cited - keys)
    reporter.add(arxiv_id, "", "citation_key_coverage", "WARN" if unmatched else "PASS",
                 f"matched={len(cited)-len(unmatched)}/{len(cited)}, missing={unmatched[:20]}")
    # A bibliography environment must never survive as a node; a mention of
    # the word 'References' in a sentence is not a structural violation.
    bad_refs = [eid for eid, row in elements.items()
                if re.search(r"\\begin\s*\{thebibliography\*?\}", row["content"])
                or (row["kind"] in {"heading", "paragraph"} and
                    row["content"].strip().casefold() in
                    {"references", "reference", "bibliography", "works cited", "literature cited"})]
    reporter.add(arxiv_id, "", "bibliography_exclusion", "FAIL" if bad_refs else "PASS",
                 f"offending_nodes={len(bad_refs)}")
    validate_source_inclusions(paper, gathering, elements, all_edges, reporter)
    for label, summary in gathering.items():
        if summary.get("problems"):
            reporter.add(arxiv_id, label, "gathering_problems", "WARN",
                         f"count={len(summary['problems'])}")
    return {"elements": len(elements), "hierarchy": len(hier_doc.get("rows", [])),
            "bibtex": len(bib_doc.get("rows", [])), "versions": len(expected_versions)}



def validate_source_inclusions(paper: dict, gathering: dict, elements: dict,
                               edges_by_version: dict, reporter: Reporter) -> None:
    """Compare explicit acknowledgements/appendix macros in compiled source files.

    Uses only gathering.files (the compilation path), never orphan TeX files.
    The check uses conservative patterns and warns on mismatches to avoid
    turning user macros/TeX variants into false hard failures.
    """
    from gatherer import read_text
    from latex_scan import strip_comments

    paper_dir = Path(paper["paper_dir"])
    for label, info in gathering.items():
        if info.get("status") != "ok":
            continue
        text_parts = []
        for rel in info.get("files", []):
            source = paper_dir / label / rel
            try:
                # Resolving before opening also protects against a malformed
                # gathering path that tries to escape the version folder.
                if not source.resolve().is_relative_to((paper_dir / label).resolve()):
                    raise ValueError("unsafe source path")
                raw, _utf8 = read_text(source)
                cleaned, _problems = strip_comments(raw)
                text_parts.append(cleaned)
            except (OSError, ValueError) as exc:
                reporter.add(paper["arxiv_id"], label, "source_inclusions_read", "WARN", str(exc))
        source = "\n".join(text_parts)
        version = int(label[1:])
        in_version = {node for child, parent in edges_by_version.get(version, [])
                      for node in (child, parent)}
        heading_text = [elements[node]["content"].casefold() for node in in_version
                        if node in elements and elements[node]["kind"] in {"heading", "paragraph"}]
        has_ack = bool(re.search(
            r"\\(?:section|chapter)\*?\s*\{\s*acknowledg(?:e)?ments?\s*\}"
            r"|\\begin\s*\{\s*(?:acks|acknowledgments|acknowledgements)\s*\}",
            source, re.I))
        has_appendix = bool(re.search(r"\\appendix(?![A-Za-z@])|\\begin\s*\{appendices\}",
                                     source))
        if has_ack:
            kept = any("acknowledg" in title for title in heading_text)
            reporter.add(paper["arxiv_id"], label, "acknowledgements_included",
                         "PASS" if kept else "WARN", "detected in compiled source")
        if has_appendix:
            kept = any("appendix" in title for title in heading_text)
            reporter.add(paper["arxiv_id"], label, "appendix_included",
                         "PASS" if kept else "WARN", "detected in compiled source")

def validate_parquet(tables_dir: Path, reporter: Reporter, required: bool = False) -> dict:
    """Check full Arrow schemas and non-null mandatory data in every Parquet shard."""
    try:
        import pyarrow.parquet as pq
        schemas = expected_arrow_schemas()
    except ImportError as exc:
        reporter.add("ALL", "", "pyarrow_available", "FAIL" if required else "WARN", str(exc))
        return {}
    counts = {}
    for name, expected in schemas.items():
        files = sorted((tables_dir / name).glob("*.parquet"))
        if not files:
            reporter.add("ALL", "", f"{name}_parquet_present", "FAIL" if required else "WARN",
                         f"no files under {tables_dir / name}")
            continue
        n, wrong = 0, []
        for filename in files:
            try:
                pf = pq.ParquetFile(filename)
                actual = pf.schema_arrow
                if not actual.equals(expected, check_metadata=False):
                    wrong.append(f"{filename.name}: schema expected {expected}, found {actual}")
                nonnull = [field.name for field in expected if not field.nullable]
                for batch in pf.iter_batches(batch_size=65536, columns=nonnull):
                    for col, fieldname in zip(batch.columns, nonnull):
                        if col.null_count:
                            wrong.append(f"{filename.name}: {fieldname} has {col.null_count} nulls")
                n += pf.metadata.num_rows
            except Exception as exc:  # corrupted file is a validation failure
                wrong.append(f"{filename.name}: {type(exc).__name__}: {exc}")
        counts[name] = n
        reporter.add("ALL", "", f"{name}_parquet_schema_and_nulls", "FAIL" if wrong else "PASS",
                     f"rows={n}, issues={wrong[:4]}")
    return counts


def run(data_dir: Path, *, limit: int = 0, require_parquet: bool = False,
        tables_dir: Path | None = None) -> tuple[Reporter, dict]:
    layout = DataLayout(data_dir)
    reporter = Reporter()
    papers = list_papers(layout)
    if not papers:
        reporter.add("ALL", "", "discovered_input_papers", "FAIL", "no papers in M1 manifest/corpus")
    if limit:
        papers = papers[:limit]
    parsed_statuses = ParseManifest(layout.logs).load()
    totals: Counter = Counter()
    for paper in papers:
        paper_id = paper["arxiv_id"]
        status = parsed_statuses.get(paper_id, {}).get("status")
        if status != "done":
            reporter.add(paper_id, "", "parse_manifest_status", "FAIL", f"status={status}")
            continue
        totals.update(validate_paper(paper, reporter))
        totals["papers_checked"] += 1
    counts = validate_parquet(tables_dir or layout.root / "tables", reporter, required=require_parquet)
    if not limit and counts:
        for name in ("elements", "hierarchy", "bibtex"):
            if name in counts:
                reporter.add("ALL", "", f"{name}_json_vs_parquet_rows",
                             "PASS" if counts[name] == totals[name] else "FAIL",
                             f"JSON={totals[name]} Parquet={counts[name]}")
    return reporter, dict(totals)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--tables-dir", type=Path)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--require-parquet", action="store_true")
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    report, totals = run(args.data_dir, limit=args.limit, require_parquet=args.require_parquet,
                         tables_dir=args.tables_dir)
    out = args.out or args.data_dir / "logs" / "validation_report_m2.csv"
    report.write(out)
    print(f"Validation: {report.counts()} | {totals} | {out}")
    return 1 if report.counts().get("FAIL", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
