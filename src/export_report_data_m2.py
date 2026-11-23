"""Export the small tables the Milestone 2 part of the Jupyter Book is built from.

The book is built by GitHub Actions, which cannot read the group's results on Google Drive, and the
parsed text must not be published before Milestone 3. This tool reads the results that
benchmark_colab_m2.ipynb saved and writes a few aggregated tables (counts, sizes, statuses, timings;
no LaTeX text, no reference titles) into the book repository. It sends no request and changes
nothing in the results.

Usage. RESULTS is the notebook's result folder on Drive (RESULT_DRIVE, "g11_m2_benchmark"): one
<range>_m2.tar.gz per range and _merged/ with group_report/, export_logs/ and tables/ (or tables.json).

    python src/export_report_data_m2.py --results /content/drive/MyDrive/g11_m2_benchmark \
        --out ../group11-arxiv-project-jupyter-book/data/m2

A range can also be given as NAME=PATH, where PATH is a <range>_m2.tar.gz or a range's data folder
(m2_work/<range> on the runtime that ran it); the order of the arguments is the order of the book:

    python src/export_report_data_m2.py Tram=/content/m2_work/Tram Thu=... \
        --merged /content/m2_work/_merged --out <book>/data/m2

Outputs in --out:
    meta.json                   export time, code commit, ranges, CPU, run ids, warnings
    members.csv                 group_benchmark_m2.csv: one row per range, then "group"
    statistics.csv              summary_statistics_m2.csv of every range (member, metric, value, unit, note)
    papers.csv.gz               one row per paper: parse_log.csv joined with reference_log.csv
    versions.csv.gz             one row per version: gathering.json and the tree shape of hierarchy.json
    headings.csv                headings counted by level (depth among headings), per range
    problems.csv                gathering problems counted by kind, per range
    validation_counts.csv       validation_report_m2.csv counted by check and status
    validation_issues.csv.gz    the FAIL and WARN rows (details cut to 200 characters)
    benchmark_runs.csv          each range's benchmark run (performance_log_m2.csv) and the export run
    performance_samples.csv.gz  RAM samples of those runs, thinned to --max-samples per run
    tables.csv                  tables.json of every range and of the merged tables
    bbl.csv                     fetch_bbl.py's results counted by status (when the range has bbl_log.csv)

Run it after the notebook's sections 8-11 (every range parsed, validated and counted, tables merged).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import tarfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ORDER = ("Tram", "Thu", "Phat", "Bao", "Quan")
LOG_FILES = ("parse_log.csv", "reference_log.csv", "validation_report_m2.csv", "summary_statistics_m2.csv",
             "performance_log_m2.csv", "bbl_log.csv")
PARSED_FILES = ("gathering.json", "hierarchy.json", "elements.json")
REF_FIELDS = ("bibitems", "amsrefs", "bib_entries_total", "bib_entries_used", "bib_uncited_dropped",
              "cited_keys", "cited_not_found", "rows_before_dedup", "rows_after_dedup", "renamed_keys",
              "key_conflicts", "cite_commands_renamed", "joined_by_key", "joined_by_doi", "joined_by_arxiv",
              "joined_by_url", "joined_by_title", "joined_by_fuzzy_title")
PAPER_FIELDS = ("member", "arxiv_id", "status", "versions", "versions_with_tex", "elements", "edges", "bibtex",
                "heading", "paragraph", "list", "item", "sentence", "formula", "figure", "missing_inputs",
                "unused_tex", "problems", "chars", "seconds", "error_type") + REF_FIELDS
VERSION_FIELDS = ("member", "arxiv_id", "version", "status", "tex_files", "multi_file", "missing_inputs",
                  "unused_tex", "problems", "bib_files", "bbl_input", "bib_fallback", "has_thebibliography",
                  "chars", "nodes", "max_depth", "max_heading_level", "heading", "paragraph", "list", "item",
                  "sentence", "formula", "figure")
KINDS = ("heading", "paragraph", "list", "item", "sentence", "formula", "figure")


# ---------------------------------------------------------------- reading a range (tar.gz or folder)

class Range:
    """The files of one range that the export needs, read once from a .tar.gz or a folder."""

    def __init__(self, name: str, path: Path):
        self.name, self.path = name, path
        self.logs: dict[str, bytes] = {}       # logs/<file> -> content
        self.other: dict[str, bytes] = {}      # m2_done.json, tables/tables.json, performance_* files
        self.papers: dict[str, dict] = {}      # paper folder -> {file name: parsed JSON}

    def wanted(self, rel: str) -> str | None:
        parts = rel.split("/")
        if len(parts) == 2 and parts[0] == "logs" and (parts[1] in LOG_FILES or parts[1].startswith("performance_")):
            return "log"
        if rel in ("m2_done.json", "tables/tables.json"):
            return "other"
        if len(parts) == 4 and parts[0] == "corpus" and parts[2] == "parsed" and parts[3] in PARSED_FILES:
            return "parsed"
        return None

    def take(self, rel: str, data: bytes, on_paper) -> None:
        kind = self.wanted(rel)
        if kind == "log":
            self.logs[rel.split("/")[1]] = data
        elif kind == "other":
            self.other[rel] = data
        elif kind == "parsed":
            folder, file = rel.split("/")[1], rel.split("/")[3]
            files = self.papers.setdefault(folder, {})
            files[file] = json.loads(data)
            if len(files) == len(PARSED_FILES):  # a paper is complete: summarise it and free the text
                on_paper(folder, self.papers.pop(folder))

    def read(self, on_paper) -> None:
        if self.path.is_dir():
            for path in sorted(self.path.rglob("*")):
                rel = path.relative_to(self.path).as_posix()
                if path.is_file() and self.wanted(rel):
                    self.take(rel, path.read_bytes(), on_paper)
        else:
            with tarfile.open(self.path, "r:*") as archive:
                for member in archive:  # streamed: one pass, no extraction
                    rel = member.name.lstrip("./")
                    if member.isfile() and self.wanted(rel):
                        self.take(rel, archive.extractfile(member).read(), on_paper)
        for folder, files in list(self.papers.items()):  # papers without one of the files
            on_paper(folder, files)
        self.papers.clear()

    def csv(self, file: str) -> list[dict]:
        data = self.logs.get(file)
        if data is None:
            return []
        return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))

    def json(self, rel: str):
        data = self.other.get(rel)
        return json.loads(data) if data is not None else None


# ---------------------------------------------------------------- per paper and per version

def tree_shape(edges: list, kinds: dict[str, str]) -> dict[int, dict]:
    """Per version: node count, depth of the deepest node, kinds, and the level of each heading
    (1 + the number of headings above it). Only kinds and ids are used, never the text."""
    by_version: dict[int, dict[str, str]] = defaultdict(dict)
    for version, child, parent in edges:
        by_version[int(version)][child] = parent
    shapes = {}
    for version, parent_of in by_version.items():
        depth, level = {}, {}

        def walk(node):  # iterative: deep trees must not hit the recursion limit
            chain = []
            while node in parent_of and node not in depth:
                chain.append(node)
                node = parent_of[node]
            base_depth = depth.get(node, 0)
            base_level = level.get(node, 0)
            for n in reversed(chain):
                base_depth += 1
                if kinds.get(n) == "heading":
                    base_level += 1
                depth[n], level[n] = base_depth, base_level
            return depth.get(chain[0], base_depth) if chain else base_depth

        for child in parent_of:
            walk(child)
        counts = Counter(kinds.get(n, "?") for n in parent_of)
        heading_levels = Counter(level[n] for n in parent_of if kinds.get(n) == "heading")
        shapes[version] = {"nodes": len(parent_of) + 1, "max_depth": max(depth.values(), default=0),
                           "max_heading_level": max(heading_levels, default=0),
                           "heading_levels": heading_levels, **{k: counts.get(k, 0) for k in KINDS}}
    return shapes


PROBLEM_KINDS = [
    (re.compile(r"milestone 1 status", re.I), "Không có TeX (theo M1)"),
    (re.compile(r"missing|not found|không có", re.I), "File được nhắc tới nhưng không có"),
    (re.compile(r"root|documentclass|begin\{document\}", re.I), "Không xác định được file gốc"),
    (re.compile(r"cycle|recurs|loop", re.I), "Vòng lặp \\input"),
    (re.compile(r"encod|decode|utf", re.I), "Lỗi mã hoá ký tự"),
    (re.compile(r"endinput|truncat", re.I), "Bị cắt bởi \\endinput"),
]


def problem_kind(text: str) -> str:
    for pattern, label in PROBLEM_KINDS:
        if pattern.search(text):
            return label
    return "Khác"


class Collector:
    def __init__(self):
        self.versions: list[dict] = []
        self.headings: Counter = Counter()
        self.problems: Counter = Counter()

    def paper(self, member: str, folder: str, files: dict) -> None:
        arxiv_id = folder.replace("-", ".", 1)
        gathering = files.get("gathering.json") or {}
        elements = files.get("elements.json") or {}
        hierarchy = files.get("hierarchy.json") or {}
        cols = elements.get("columns") or ["element_id", "kind", "content"]
        i_id, i_kind = cols.index("element_id"), cols.index("kind")
        kinds = {row[i_id]: row[i_kind] for row in elements.get("rows") or []}
        hcols = hierarchy.get("columns") or ["version", "element_id", "parent_id"]
        order = [hcols.index(c) for c in ("version", "element_id", "parent_id")]
        edges = [[row[k] for k in order] for row in hierarchy.get("rows") or []]
        shapes = tree_shape(edges, kinds)
        for label, g in sorted(gathering.items(), key=lambda kv: int(re.sub(r"\D", "", kv[0]) or 0)):
            n = int(re.sub(r"\D", "", label) or 0)
            shape = shapes.get(n, {})
            for problem in g.get("problems") or []:
                self.problems[(member, problem_kind(str(problem)))] += 1
            for lvl, count in (shape.get("heading_levels") or {}).items():
                self.headings[(member, lvl)] += count
            files_n = len(g.get("files") or [])
            self.versions.append({
                "member": member, "arxiv_id": arxiv_id, "version": n, "status": g.get("status"),
                "tex_files": files_n, "multi_file": int(files_n > 1),
                "missing_inputs": len(g.get("missing") or []), "unused_tex": len(g.get("unused") or []),
                "problems": len(g.get("problems") or []), "bib_files": len(g.get("bib_files") or []),
                "bbl_input": int(bool(g.get("bbl_input"))), "bib_fallback": int(bool(g.get("bib_fallback"))),
                "has_thebibliography": int(bool(g.get("has_thebibliography"))), "chars": g.get("chars"),
                "nodes": shape.get("nodes", 0), "max_depth": shape.get("max_depth", 0),
                "max_heading_level": shape.get("max_heading_level", 0),
                **{k: shape.get(k, 0) for k in KINDS}})


# ---------------------------------------------------------------- helpers

def write_csv(path: Path, fields, rows) -> None:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def latest_by_id(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for row in rows:  # logs are appended: the last row of an ID is its final state
        if row.get("arxiv_id"):
            out[row["arxiv_id"]] = row
    return out


def error_type(error: str) -> str:
    if not error:
        return ""
    return re.split(r"[:(\s]", error.strip(), maxsplit=1)[0][:40]


def thin(rows: list[dict], limit: int) -> list[dict]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    keep = {int(i * step) for i in range(limit)} | {len(rows) - 1}
    return [r for i, r in enumerate(rows) if i in keep]


def find_ranges(results: Path) -> list[tuple[str, Path]]:
    found = {p.name[:-len("_m2.tar.gz")]: p for p in results.glob("*_m2.tar.gz")}
    ordered = [n for n in DEFAULT_ORDER if n in found] + sorted(n for n in found if n not in DEFAULT_ORDER)
    return [(n, found[n]) for n in ordered]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("ranges", nargs="*", help="NAME=PATH (a <range>_m2.tar.gz or a range's data folder)")
    p.add_argument("--results", type=Path, help="the notebook's result folder (g11_m2_benchmark)")
    p.add_argument("--merged", type=Path, help="the _merged folder (default: RESULTS/_merged)")
    p.add_argument("--out", type=Path, required=True, help="<book>/data/m2")
    p.add_argument("--max-samples", type=int, default=400, help="RAM samples kept per run")
    p.add_argument("--max-issues", type=int, default=20000, help="FAIL/WARN rows kept per range")
    args = p.parse_args(argv)
    ranges = []
    for item in args.ranges:
        name, _, path = item.partition("=")
        if not path:
            p.error(f"expected NAME=PATH, got {item!r}")
        ranges.append((name, Path(path)))
    if args.results:
        ranges = ranges or find_ranges(args.results)
        args.merged = args.merged or args.results / "_merged"
    if not ranges:
        p.error("give --results or NAME=PATH arguments")
    for _, path in ranges:
        if not path.exists():
            p.error(f"not found: {path}")
    args.ranges = ranges
    return args


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    args = parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    collector = Collector()
    stats, papers, val_counts, val_issues, runs, samples, tables, bbl = [], [], Counter(), [], [], [], [], Counter()
    done_info, issues_kept = {}, Counter()

    for name, path in args.ranges:
        print(f"[{name}] reading {path}", flush=True)
        rng = Range(name, path)
        rng.read(lambda folder, files, name=name: collector.paper(name, folder, files))
        done = rng.json("m2_done.json")
        if done is None:
            warnings.append(f"{name}: no m2_done.json, the range was not finished by the notebook")
        else:
            done_info[name] = done
        for file in ("parse_log.csv", "reference_log.csv", "validation_report_m2.csv", "summary_statistics_m2.csv",
                     "performance_log_m2.csv"):
            if file not in rng.logs:
                warnings.append(f"{name}: logs/{file} is missing")

        stats += [{"member": name, **r} for r in rng.csv("summary_statistics_m2.csv")]

        refs = latest_by_id(rng.csv("reference_log.csv"))
        for arxiv_id, row in latest_by_id(rng.csv("parse_log.csv")).items():
            ref = refs.get(arxiv_id, {})
            papers.append({**row, "member": name, "error_type": error_type(row.get("error", "")),
                           **{k: ref.get(k, "") for k in REF_FIELDS}})

        for row in rng.csv("validation_report_m2.csv"):
            val_counts[(name, row.get("check_name"), row.get("status"))] += 1
            if row.get("status") in ("FAIL", "WARN") and issues_kept[name] < args.max_issues:
                issues_kept[name] += 1
                val_issues.append({"member": name, "arxiv_id": row.get("arxiv_id"), "version": row.get("version"),
                                   "check_name": row.get("check_name"), "status": row.get("status"),
                                   "details": (row.get("details") or "")[:200]})

        bench = rng.csv("performance_log_m2.csv")
        run = next((r for r in reversed(bench) if done and r.get("run_id") == done.get("run_id")), bench[-1] if bench else None)
        if run:
            runs.append({"member": name, "stage": "parse", **run})
            sample_file = f"performance_samples_{run['run_id']}.csv"
            rows = rng.csv(sample_file)
            if not rows:
                warnings.append(f"{name}: logs/{sample_file} is missing (no RAM curve)")
            samples += [{"member": name, "run_id": run["run_id"], **r} for r in thin(rows, args.max_samples)]

        t = rng.json("tables/tables.json")
        if t:
            tables.append({"member": name, **t})
        for row in rng.csv("bbl_log.csv"):
            bbl[(name, row.get("status") or "")] += 1

    group_rows, group_json, export_summary = [], {}, None
    if args.merged and args.merged.is_dir():
        gb = args.merged / "group_report" / "group_benchmark_m2.csv"
        if gb.is_file():
            group_rows = list(csv.DictReader(gb.open(encoding="utf-8-sig")))
            for row in group_rows:
                row["member"] = "group" if row.get("member", "").upper() == "GROUP" else row.get("member")
                row.pop("data_dir", None)  # Drive or runtime paths do not belong in the book
        else:
            warnings.append("no _merged/group_report/group_benchmark_m2.csv: run the notebook's section 10")
        gj = args.merged / "group_report" / "group_benchmark_m2.json"
        if gj.is_file():
            group_json = json.loads(gj.read_text(encoding="utf-8"))
            warnings += [f"group_benchmark_m2: {w}" for w in group_json.get("warnings") or []]
        logs = args.merged / "export_logs"
        summaries = sorted(logs.glob("performance_summary_*.json")) if logs.is_dir() else []
        if summaries:
            export_summary = json.loads(summaries[-1].read_text(encoding="utf-8"))
            runs.append({"member": "export", "stage": "export", "run_id": export_summary.get("run_id"),
                         "status": "finished" if export_summary.get("return_code") == 0 else "failed",
                         "exit_code": export_summary.get("return_code"),
                         "wall_seconds": export_summary.get("wall_seconds"),
                         "ram_sampled_peak_bytes": export_summary.get("ram_sampled_peak_bytes")
                         or export_summary.get("ram_peak_bytes"),
                         "ram_mean_bytes": export_summary.get("ram_mean_bytes")})
            sample_csv = logs / f"performance_samples_{export_summary.get('run_id')}.csv"
            if sample_csv.is_file():
                rows = list(csv.DictReader(sample_csv.open(encoding="utf-8-sig")))
                samples += [{"member": "export", "run_id": export_summary.get("run_id"), **r}
                            for r in thin(rows, args.max_samples)]
        else:
            warnings.append("no _merged/export_logs/performance_summary_*.json: the merge was not measured")
        merged_tables = args.merged / "tables" / "tables.json"
        if not merged_tables.is_file():
            merged_tables = args.merged / "tables.json"
        if merged_tables.is_file():
            tables.append({"member": "merged", **json.loads(merged_tables.read_text(encoding="utf-8"))})
    else:
        warnings.append("no _merged folder: the group row and the merged tables are not exported")

    for name, _ in args.ranges:
        if not any(r["member"] == name for r in papers):
            warnings.append(f"{name}: no paper rows (parse_log.csv empty)")

    write_csv(args.out / "members.csv", list(group_rows[0].keys()) if group_rows else ["member"], group_rows)
    write_csv(args.out / "statistics.csv", ["member", "metric", "value", "unit", "scope", "source", "note"], stats)
    write_csv(args.out / "papers.csv.gz", PAPER_FIELDS, papers)
    write_csv(args.out / "versions.csv.gz", VERSION_FIELDS, collector.versions)
    write_csv(args.out / "headings.csv", ["member", "level", "headings"],
              [{"member": m, "level": lvl, "headings": n} for (m, lvl), n in sorted(collector.headings.items())])
    write_csv(args.out / "problems.csv", ["member", "kind", "versions"],
              [{"member": m, "kind": k, "versions": n} for (m, k), n in sorted(collector.problems.items())])
    write_csv(args.out / "validation_counts.csv", ["member", "check_name", "status", "n"],
              [{"member": m, "check_name": c, "status": s, "n": n} for (m, c, s), n in sorted(val_counts.items())])
    write_csv(args.out / "validation_issues.csv.gz",
              ["member", "arxiv_id", "version", "check_name", "status", "details"], val_issues)
    write_csv(args.out / "benchmark_runs.csv",
              ["member", "stage", "run_id", "status", "exit_code", "wall_seconds", "ram_sampled_peak_bytes",
               "ram_mean_bytes", "parquet_final_bytes", "workers", "force_reparse", "cpu_only_claim", "environment"],
              runs)
    write_csv(args.out / "performance_samples.csv.gz",
              ["member", "run_id", "elapsed_seconds", "rss_tree_bytes", "process_count"], samples)
    write_csv(args.out / "tables.csv",
              ["member", "papers", "elements_rows", "elements_bytes", "hierarchy_rows", "hierarchy_bytes",
               "bibtex_rows", "bibtex_bytes"], tables)
    write_csv(args.out / "bbl.csv", ["member", "status", "versions"],
              [{"member": m, "status": s, "versions": n} for (m, s), n in sorted(bbl.items())])

    commits = sorted({d.get("commit") for d in done_info.values() if d.get("commit")})
    if len(commits) > 1:
        warnings.append(f"the ranges were parsed with different code commits: {commits}")
    meta = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "members": [n for n, _ in args.ranges],
        "commits": commits,
        "ranges": {n: {k: d.get(k) for k in ("finished_utc", "commit", "cpu_model", "cpu_logical", "workers", "run_id")}
                   for n, d in done_info.items()},
        "export_run": {k: export_summary.get(k) for k in ("run_id", "wall_seconds", "return_code", "started_utc")}
        if export_summary else None,
        "group_definitions": group_json.get("definitions"),
        "warnings": warnings,
    }
    (args.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.out}: {len(papers):,} papers, {len(collector.versions):,} versions")
    for w in warnings:
        print("WARNING:", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
