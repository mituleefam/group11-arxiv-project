"""Member 4 (M2): auditable statistics from canonical per-paper parsed JSONs.

    python src/statistics_m2.py --data-dir data
    python src/statistics_m2.py --data-dir data --limit 5

Important: 'before element deduplication' means node OCCURRENCES (one per
hierarchy edge, plus one root per version). 'after' means distinct element
rows. The ratio is a record-count reduction, not a disk compression ratio.
Reference 'before' counts come from Member 3's separate reference_log.csv.
They are explicitly marked missing when that log is unavailable.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from checkpoint import ParseManifest
from common import DataLayout, Manifest
from pipeline_m2 import list_papers

KINDS = ("document", "heading", "paragraph", "list", "item", "sentence", "formula", "figure")
FIELDS = ("metric", "value", "unit", "scope", "source", "note")


def _latest_csv(path: Path) -> dict[str, dict]:
    latest = {}
    if not path.is_file():
        return latest
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("arxiv_id"):
                latest[row["arxiv_id"]] = row
    return latest


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None



def compare_element_parquet_bytes(data_dir: Path) -> tuple[int, int] | None:
    """Counterfactual storage comparison using zstd + the SAME Arrow schema.

    Rebuild an *underdeduplicated* elements Parquet from each hierarchy edge
    plus one root per version. Returns (counterfactual bytes, actual bytes).
    This is a controlled comparison, not a historically measured output file.
    Different ordering/grouping can affect zstd compression, so report as an
    approximation and keep it separate from the official M2 benchmark.
    """
    import tempfile
    import pyarrow as pa
    import pyarrow.parquet as pq
    from validate_m2 import expected_arrow_schemas

    layout = DataLayout(data_dir)
    table_folder = layout.root / "tables" / "elements"
    actual_files = sorted(table_folder.glob("*.parquet"))
    if not actual_files:
        return None
    manifest = ParseManifest(layout.logs).load()
    schema = expected_arrow_schemas()["elements"]
    with tempfile.TemporaryDirectory(prefix="role4_counterfactual_") as temporary:
        dest = Path(temporary) / "elements_before.parquet"
        writer = pq.ParquetWriter(dest, schema, compression="zstd")
        columns = {name: [] for name in schema.names}
        chars = 0

        def flush():
            nonlocal chars
            if not columns["element_id"]:
                return
            arrays = []
            for field in schema:
                values = columns[field.name]
                if pa.types.is_dictionary(field.type):
                    arrays.append(pa.array(values, type=pa.string()).dictionary_encode())
                else:
                    arrays.append(pa.array(values, type=field.type))
            writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
            for values in columns.values():
                values.clear()
            chars = 0

        def add(row, paper_id):
            nonlocal chars
            columns["element_id"].append(row[0])
            columns["arxiv_id"].append(paper_id)
            columns["kind"].append(row[1])
            columns["content"].append(row[2])
            chars += sum(len(value) for value in (row[0], paper_id, row[1], row[2]))
            if len(columns["element_id"]) >= 200000 or chars >= 500000000:
                flush()

        try:
            for paper in list_papers(layout):
                if manifest.get(paper["arxiv_id"], {}).get("status") != "done":
                    continue
                parsed = Path(paper["paper_dir"]) / "parsed"
                with (parsed / "elements.json").open(encoding="utf-8") as f:
                    elements = json.load(f)["rows"]
                with (parsed / "hierarchy.json").open(encoding="utf-8") as f:
                    edges = json.load(f)["rows"]
                by_id = {row[0]: row for row in elements}
                for row in elements:
                    if row[1] == "document":
                        add(row, paper["arxiv_id"])
                for version, child, parent in edges:
                    add(by_id[child], paper["arxiv_id"])
            flush()
        finally:
            writer.close()
        return (dest.stat().st_size, sum(p.stat().st_size for p in actual_files))

def collect(data_dir: Path, *, limit: int = 0,
            counterfactual_parquet: bool = False) -> list[dict]:
    layout = DataLayout(data_dir)
    papers = list_papers(layout)
    if limit:
        papers = papers[:limit]
    manifest = ParseManifest(layout.logs).load()
    refs = _latest_csv(layout.logs / "reference_log.csv")
    m1_manifest = Manifest(layout.manifest).load()
    counters = Counter()
    error_types = Counter()
    version_status = Counter()
    kinds_unique = Counter()
    kinds_occurrences = Counter()
    ref_before = 0
    ref_log_papers = 0
    parse_seconds = []
    for p in papers:
        paper_id = p["arxiv_id"]
        entry = manifest.get(paper_id, {})
        if entry.get("status") != "done":
            counters["papers_not_parsed"] += 1
            error = str(entry.get("error", ""))
            label = error.split(":", 1)[0].strip() if error else "not_run_or_unknown"
            error_types[label] += 1
            continue
        parsed = Path(p["paper_dir"]) / "parsed"
        try:
            with (parsed / "elements.json").open(encoding="utf-8") as f:
                elements = json.load(f)["rows"]
            with (parsed / "hierarchy.json").open(encoding="utf-8") as f:
                edges = json.load(f)["rows"]
            with (parsed / "bibtex.json").open(encoding="utf-8") as f:
                bibtex = json.load(f)["rows"]
            with (parsed / "gathering.json").open(encoding="utf-8") as f:
                gathering = json.load(f)
        except (OSError, ValueError, KeyError, TypeError):
            counters["papers_missing_or_invalid_output"] += 1
            continue
        by_id = {r[0]: r[1] for r in elements if len(r) == 3}
        root_occ = sum(kind == "document" for kind in by_id.values())
        counters["papers_with_output"] += 1
        counters["versions_with_record"] += len(gathering)
        counters["elements_unique"] += len(elements)
        counters["elements_occurrences_before_dedup"] += len(edges) + root_occ
        counters["hierarchy_edges"] += len(edges)
        counters["bibtex_unique"] += len(bibtex)
        for e in elements:
            if len(e) == 3:
                kinds_unique[e[1]] += 1
        for _, child, _parent in edges:
            kinds_occurrences[by_id.get(child, "unknown")] += 1
        kinds_occurrences["document"] += root_occ
        for status in gathering.values():
            version_status[str(status.get("status", "unknown"))] += 1
        for row in bibtex:
            for field in ("title", "authors", "year", "doi", "journal", "url"):
                if row.get(field) not in (None, "", []):
                    counters[f"bibtex_with_{field}"] += 1
        seconds = _float(entry.get("seconds"))
        if seconds is not None:
            parse_seconds.append(seconds)
        ref_row = refs.get(paper_id)
        if ref_row and ref_row.get("status") == "done":
            value = _int(ref_row.get("rows_before_dedup"))
            if value is not None:
                ref_before += value
                ref_log_papers += 1
    output = []

    def add(key, value, unit="count", scope="selected_discovered_papers", source="parsed_json", note=""):
        output.append({"metric": key, "value": value if value is not None else "",
                       "unit": unit, "scope": scope, "source": source, "note": note})

    def percentage(numer, denom):
        return round(100.0 * numer / denom, 4) if denom else None

    add("discovered_papers_selected", len(papers), source="M1_manifest_or_corpus",
        note="Not the full assigned ID range; unavailable papers may be excluded by M1 discovery")
    add("m1_manifest_entries", len(m1_manifest), source="M1_manifest",
        note="May include unsuccessful/non-existent assigned IDs; not necessarily all assigned IDs")
    add("papers_with_output", counters["papers_with_output"])
    add("papers_not_parsed", counters["papers_not_parsed"], source="parse_manifest")
    add("papers_missing_or_invalid_output", counters["papers_missing_or_invalid_output"])
    for error_type, n in sorted(error_types.items()):
        add(f"parse_failure_type_{error_type}", n, source="parse_manifest",
            note="Exception class as recorded; not a verified semantic root cause")
    add("parsing_success_percent", percentage(counters["papers_with_output"], len(papers)), "%",
        source="parse_manifest_and_parsed_json", note="Denominator: M1-discovered papers selected")
    add("versions_with_record", counters["versions_with_record"])
    for status in sorted(version_status):
        add(f"version_status_{status}", version_status[status])
    add("versions_with_tex", version_status["ok"])
    for status, count in sorted(Counter(str(v.get("status", "unknown")) for v in m1_manifest.values()).items()):
        add(f"m1_discovery_status_{status}", count, source="M1_manifest")
    add("elements_occurrences_before_dedup", counters["elements_occurrences_before_dedup"],
        note="One per hierarchy edge + each version's root")
    add("elements_unique_after_dedup", counters["elements_unique"])
    add("element_reduction_percent",
        percentage(counters["elements_occurrences_before_dedup"] - counters["elements_unique"],
                   counters["elements_occurrences_before_dedup"]), "%",
        note="Record-count reduction, NOT compressed bytes saved")
    add("hierarchy_edges", counters["hierarchy_edges"])
    for kind in sorted(set(KINDS) | set(kinds_unique) | set(kinds_occurrences)):
        add(f"element_distinct_kind_{kind}", kinds_unique[kind])
        add(f"element_occurrences_kind_{kind}", kinds_occurrences[kind])
    add("bibtex_entries_after_dedup", counters["bibtex_unique"])
    add("bibtex_before_log_papers", ref_log_papers, source="reference_log.csv")
    add("bibtex_entries_before_dedup", ref_before if ref_log_papers == counters["papers_with_output"] else None,
        source="reference_log.csv", note="Missing unless every successfully parsed paper has a completed M3 reference log; rerun it after code changes")
    add("bibtex_reduction_percent",
        percentage(ref_before - counters["bibtex_unique"], ref_before)
        if ref_log_papers == counters["papers_with_output"] and ref_before else None, "%",
        source="reference_log.csv + parsed_json")
    for field in ("title", "authors", "year", "doi", "journal", "url"):
        add(f"bibtex_{field}_completeness_percent",
            percentage(counters[f"bibtex_with_{field}"], counters["bibtex_unique"]), "%")
    add("mean_parse_seconds_per_paper", round(sum(parse_seconds) / len(parse_seconds), 3)
        if parse_seconds else None, "seconds", source="parse_manifest",
        note="Per-paper worker time; NOT end-to-end wall time under multiprocessing")
    add("parsing_time_logged_papers", len(parse_seconds), source="parse_manifest")
    # Validation errors/warnings are separate from parser exceptions. Treat
    # their names as categories rather than inventing a semantic root cause.
    val_path = layout.logs / "validation_report_m2.csv"
    if val_path.is_file():
        validation = Counter()
        by_check = Counter()
        selected_ids = {p["arxiv_id"] for p in papers}
        with val_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("arxiv_id") not in selected_ids and row.get("arxiv_id") != "ALL":
                    continue
                status = row.get("status", "UNKNOWN")
                validation[status] += 1
                if status in ("FAIL", "WARN"):
                    by_check[(row.get("check_name", "unnamed"), status)] += 1
        for status in ("PASS", "WARN", "FAIL"):
            add(f"validation_{status.lower()}_checks", validation[status], source="validation_report_m2.csv",
                note="Count of CHECK RECORDS, not count of publications")
        for (check, status), count in sorted(by_check.items()):
            add(f"validation_{status.lower()}_{check}", count, source="validation_report_m2.csv")
    else:
        add("validation_results_available", 0, source="validation_report_m2.csv",
            note="Run validate_m2.py to populate this section")
    table_dir = layout.root / "tables"
    for table in ("elements", "hierarchy", "bibtex"):
        files = list((table_dir / table).glob("*.parquet"))
        add(f"{table}_parquet_bytes", sum(p.stat().st_size for p in files) if files else None,
            "bytes", "selected_data_root", "parquet_files",
            "Measured on disk; may contain more papers than a --limit selection")
    if counterfactual_parquet:
        if limit:
            raise ValueError("Counterfactual requires full selection: omit --limit")
        try:
            sizes = compare_element_parquet_bytes(data_dir)
        except ImportError as exc:
            raise RuntimeError("Counterfactual Parquet comparison requires pyarrow") from exc
        if sizes is None:
            raise ValueError("Parquet elements output missing; run full M2 export first")
        hypothetical, actual = sizes
        note = ("COUNTERFACTUAL estimate, not a measured historical before-dedup output; "
                "zstd compression is sensitive to row order/batch grouping")
        add("elements_counterfactual_without_dedup_parquet_bytes", hypothetical, "bytes",
            "full_export", "counterfactual_parquet", note)
        add("elements_actual_with_dedup_parquet_bytes", actual, "bytes", "full_export",
            "measured_parquet_files", "Actual final output of M2 pipeline")
        add("elements_counterfactual_parquet_reduction_percent",
            percentage(hypothetical - actual, hypothetical), "%", "full_export",
            "counterfactual_parquet", note)
    return output


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--out", type=Path)
    p.add_argument("--counterfactual-parquet", action="store_true",
                   help="Reconstruct hypothetical pre-dedup elements Parquet (requires full export)")
    args = p.parse_args(argv)
    rows = collect(args.data_dir, limit=args.limit, counterfactual_parquet=args.counterfactual_parquet)
    path = args.out or args.data_dir / "logs" / "summary_statistics_m2.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Statistics: {len(rows)} metrics -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
