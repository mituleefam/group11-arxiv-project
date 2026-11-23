"""Aggregate *measured* M2 runs from all assigned member data directories.

Use one completed, forced full-corpus benchmark per member (the latest such
run). The 5 ranges are processed sequentially, hence wall time is the SUM of
those runs plus the measured end-to-end --export-only merge. RAM peak is the
MAX of runs, never their sum. Final disk footprint is measured from the merged
Parquet files (not the sum of 5 member outputs). Percentages are re-computed
from aggregated counts, never added/averaged.

Example (after benchmarking each member with benchmark_m2.py --force):
 python src/group_benchmark.py --m2 T1=/content/m1 T2=/content/m2 \\
   --tables-dir /content/merged_tables \\
   --export-summary /content/logs/performance_summary_EXPORT.json \\
   --out /content/group_report

The export summary must come from benchmarking pipeline_m2.py --export-only,
not from an unrelated crawl. The summary is explicitly user-supplied, not
independently verifiable from the JSON alone: retain command/runtime metadata.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

METRIC_KEYS = (
    "discovered_papers_selected", "papers_with_output", "papers_not_parsed",
    "papers_missing_or_invalid_output", "versions_with_record", "versions_with_tex",
    "elements_occurrences_before_dedup", "elements_unique_after_dedup",
    "hierarchy_edges", "bibtex_entries_before_dedup", "bibtex_entries_after_dedup",
    "validation_pass_checks", "validation_warn_checks", "validation_fail_checks",
)
SUM_FIELDS = {"papers", "versions", "versions_with_tex", "element_before", "element_after",
              "hierarchy_edges", "bibtex_before", "bibtex_after", "validation_fail"}
COLUMNS = ("member", "data_dir", "papers", "versions", "versions_with_tex",
           "element_before", "element_after", "element_reduction_percent",
           "hierarchy_edges", "bibtex_before", "bibtex_after", "bibtex_reduction_percent",
           "wall_seconds", "ram_peak_bytes", "parquet_final_bytes",
           "validation_fail", "benchmark_run_id", "completeness")


def _number(s):
    try:
        return float(s) if s not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _csv(path: Path):
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _pct(before, after):
    if before is None or after is None or before == 0:
        return None
    return round(100 * (before - after) / before, 4)


def _latest_complete(root: Path):
    rows = _csv(root / "logs" / "performance_log_m2.csv")
    full = [r for r in rows if str(r.get("paper_limit", "")).lower() == "all"
            and str(r.get("force_reparse", "")) in ("1", "true", "True")
            and r.get("status") == "finished" and str(r.get("exit_code")) == "0"]
    return max(full, key=lambda x: (x.get("ended_utc") or "", x.get("run_id") or "")) if full else None


def _metrics(root: Path):
    rows = _csv(root / "logs" / "summary_statistics_m2.csv")
    return {r.get("metric"): _number(r.get("value")) for r in rows if r.get("metric")}


def _member(name: str, root: Path, warnings: list[str]):
    stats, bench = _metrics(root), _latest_complete(root)
    if not bench:
        warnings.append(f"{name}: no completed --force full benchmark in performance_log_m2.csv")
    if not stats:
        warnings.append(f"{name}: missing summary_statistics_m2.csv")
    stats_path = root / "logs" / "summary_statistics_m2.csv"
    bench_path = root / "logs" / "performance_log_m2.csv"
    stats_fresh = (stats_path.exists() and bench_path.exists() and
                   stats_path.stat().st_mtime >= bench_path.stat().st_mtime)
    if not stats_fresh:
        warnings.append(f"{name}: statistics are older than the benchmark log (or missing); "
                        "rerun reference_stats.py, validate_m2.py and statistics_m2.py")
    total = stats.get("discovered_papers_selected")
    complete = stats.get("papers_with_output")
    bad = (stats.get("papers_not_parsed") or 0) + (stats.get("papers_missing_or_invalid_output") or 0)
    if total is None or complete is None or complete != total or bad:
        warnings.append(f"{name}: coverage incomplete or not measured: selected={total}, good={complete}, errors={bad}")
    if stats.get("bibtex_entries_before_dedup") is None:
        warnings.append(f"{name}: reference 'before' counts missing (run reference_stats.py)")
    if (stats.get("validation_fail_checks") or 0) > 0:
        warnings.append(f"{name}: validation has FAIL checks")
    row = {
        "member": name, "data_dir": str(root), "papers": complete,
        "versions": stats.get("versions_with_record"),
        "versions_with_tex": stats.get("versions_with_tex"),
        "element_before": stats.get("elements_occurrences_before_dedup"),
        "element_after": stats.get("elements_unique_after_dedup"),
        "hierarchy_edges": stats.get("hierarchy_edges"),
        "bibtex_before": stats.get("bibtex_entries_before_dedup"),
        "bibtex_after": stats.get("bibtex_entries_after_dedup"),
        "wall_seconds": _number(bench.get("wall_seconds")) if bench else None,
        "ram_peak_bytes": _number(bench.get("ram_sampled_peak_bytes")) if bench else None,
        "parquet_final_bytes": None,  # member outputs are NOT the group final footprint
        "validation_fail": stats.get("validation_fail_checks"),
        "benchmark_run_id": bench.get("run_id") if bench else None,
        "completeness": "complete" if bench and total == complete and not bad and
                              stats.get("validation_fail_checks") == 0 and
                              stats.get("bibtex_entries_before_dedup") is not None and
                              stats_fresh else "incomplete",
    }
    row["element_reduction_percent"] = _pct(row["element_before"], row["element_after"])
    row["bibtex_reduction_percent"] = _pct(row["bibtex_before"], row["bibtex_after"])
    return row


def aggregate(members: list[tuple[str, Path]], tables_dir: Path | None,
              export_summary: Path | None, expected_per_member: int | None = None) -> tuple[list[dict], dict, list[str]]:
    """Return per-member rows, group row and audit warnings; no writes."""
    warnings: list[str] = []
    if len({name for name, _ in members}) != len(members):
        raise ValueError("member names must be distinct")
    roots = [root.resolve() for _, root in members]
    if len(set(roots)) != len(roots):
        raise ValueError("same data directory supplied more than once")
    rows = [_member(name, root, warnings) for name, root in members]
    if expected_per_member is not None:
        for row in rows:
            if row["papers"] != expected_per_member:
                warnings.append(f"{row['member']}: parsed={row['papers']}, expected={expected_per_member}; "
                                "cannot report full-range success")
                row["completeness"] = "incomplete"
    group = {"member": "GROUP", "data_dir": " | ".join(str(p) for p in roots),
             "benchmark_run_id": None, "completeness": "incomplete"}
    for field in SUM_FIELDS:
        values = [r.get(field) for r in rows]
        group[field] = sum(values) if all(v is not None for v in values) else None
    group["element_reduction_percent"] = _pct(group["element_before"], group["element_after"])
    group["bibtex_reduction_percent"] = _pct(group["bibtex_before"], group["bibtex_after"])
    export = None
    if export_summary:
        try:
            export = json.loads(export_summary.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.append(f"export benchmark summary unreadable: {exc}")
    else:
        warnings.append("export benchmark summary missing; GROUP wall time incomplete")
    if export is not None:
        command = export.get("command") or []
        if (not isinstance(command, list) or "--export-only" not in command
                or "pipeline_m2.py" not in " ".join(command)
                or export.get("return_code") != 0
                or export.get("interrupted") is True):
            warnings.append("export summary does not prove a successful pipeline_m2.py "
                            "--export-only measurement; verify command/return_code")
            export = None
    export_wall = _number(export.get("wall_seconds")) if export else None
    times = [r["wall_seconds"] for r in rows]
    group["wall_seconds"] = sum(times) + export_wall if export_wall is not None and all(
        v is not None for v in times) else None
    peaks = [r["ram_peak_bytes"] for r in rows]
    if export:
        peaks.append(_number(export.get("ram_sampled_peak_bytes")))
    group["ram_peak_bytes"] = max(peaks) if peaks and all(v is not None for v in peaks) else None
    files = sorted(tables_dir.rglob("*.parquet")) if tables_dir and tables_dir.exists() else []
    if not files or not all(any(p.parent.name == name for p in files)
                            for name in ("elements", "hierarchy", "bibtex")):
        warnings.append("merged Parquet tables missing/incomplete; do not sum member Parquet bytes")
        group["parquet_final_bytes"] = None
    else:
        group["parquet_final_bytes"] = sum(file.stat().st_size for file in files)
    if all(r["completeness"] == "complete" for r in rows) and group["wall_seconds"] is not None and \
            group["ram_peak_bytes"] is not None and group["parquet_final_bytes"] is not None:
        group["completeness"] = "complete"
    return rows, group, warnings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("members", nargs="+", help="NAME=DATA_DIR (or just DATA_DIR)")
    p.add_argument("--expected-per-member", type=int, required=True,
                   help="assigned paper count per range (verify against Moodle, e.g. 5000)")
    p.add_argument("--tables-dir", type=Path, required=True)
    p.add_argument("--export-summary", type=Path, required=True,
                   help="performance_summary_*.json measured around --export-only")
    p.add_argument("--out", type=Path, default=Path("group_benchmark_m2"))
    args = p.parse_args(argv)
    members = [(x.split("=", 1)[0], Path(x.split("=", 1)[1])) if "=" in x else (Path(x).name, Path(x))
               for x in args.members]
    if args.expected_per_member < 1:
        p.error("--expected-per-member must be positive")
    try:
        rows, group, warnings = aggregate(members, args.tables_dir, args.export_summary,
                                          expected_per_member=args.expected_per_member)
    except ValueError as exc:
        p.error(str(exc))
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "group_benchmark_m2.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows + [group])
    (args.out / "group_benchmark_m2.json").write_text(json.dumps(
        {"members": rows, "group": group, "warnings": warnings,
         "definitions": {"wall_seconds": "sum of one latest completed forced full parse per member + measured export-only wall time",
                         "ram_peak_bytes": "maximum sampled process tree RSS including export stage",
                         "parquet_final_bytes": "sum of final merged Parquet shard sizes only",
                         "dedup_percent": "100 x (sum(before) - sum(after)) / sum(before)"},
         "export_summary": str(args.export_summary)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("M2 group summary:", json.dumps(group, ensure_ascii=False))
    for warning in warnings:
        print("WARNING:", warning, file=sys.stderr)
    return 0 if group["completeness"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
