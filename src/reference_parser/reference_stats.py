"""Member 3 - Milestone 2: run the reference steps alone and log what they did.

pipeline_m2.py already calls the four Member 3 modules for every paper and
writes parsed/bibtex.json and parsed/cite_rename_map.json. This script runs
the same steps (gatherer -> bibitem_parser -> bibtex_converter ->
dedup_bibtex -> citation_updater) without building the hierarchy, so the
reference part can be checked and counted quickly, and gives the numbers the
report asks for (entries before / after deduplication, fields found):

    python src/reference_parser/reference_stats.py --data-dir data --test          # 5 papers
    python src/reference_parser/reference_stats.py --data-dir data                 # every paper
    python src/reference_parser/reference_stats.py --data-dir data --ids 2604.05535 --show

Writes, in <data-dir>/logs/:
    reference_log.csv       one row per paper and run (columns: LOG_FIELDS)
    reference_versions.csv  one row per version: where its references came from
    reference_m2.log        warnings and notes of the modules, with the paper id

--show prints the deduplicated BibTeX rows and the key renames of each paper.
Nothing in corpus/ is changed.
"""

from __future__ import annotations

import argparse
import contextvars
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # src/, when run as a script

from reference_parser import bibitem_parser
from reference_parser import bibtex_converter
from reference_parser import citation_updater
from reference_parser import dedup_bibtex
from common import CsvLog, DataLayout, now_iso
from gatherer import gather, read_text

PAPER = contextvars.ContextVar("paper", default="-")
LOG_FIELDS = ["time", "arxiv_id", "status", "versions", "versions_with_tex", "versions_with_refs",
              "bibitems", "amsrefs", "bib_entries_total", "bib_entries_used", "bib_uncited_dropped",
              "bib_fallback_versions", "cited_keys", "cited_not_found", "rows_before_dedup",
              "rows_after_dedup", "renamed_keys", "key_conflicts", "cite_commands_renamed",
              "joined_by_key", "joined_by_doi", "joined_by_arxiv", "joined_by_url", "joined_by_title",
              "joined_by_fuzzy_title", "with_title", "with_authors", "with_year", "with_doi",
              "with_journal", "with_url", "misc", "convert_failed", "seconds", "error"]
VERSION_FIELDS = ["time", "arxiv_id", "version", "gather_status", "bib_files", "bib_fallback",
                  "has_thebibliography", "bibitems", "amsrefs", "bib_entries_total", "bib_entries_used",
                  "cited_keys", "cited_not_found", "rows"]


class _PaperFilter(logging.Filter):
    def filter(self, record):
        record.paper = PAPER.get()
        return True


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "reference_m2.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(paper)s] %(message)s"))
    handler.addFilter(_PaperFilter())
    logger = logging.getLogger("m2.references")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False


def references_of_paper(arxiv_id: str, paper_dir: Path, versions: list[str],
                        version_log: CsvLog | None = None) -> tuple[dict, list[dict], dict]:
    """The Member 3 steps on one paper: (counts, bibtex rows, rename map)."""
    from pipeline_m2 import VERSION, version_statuses
    statuses = version_statuses(paper_dir)
    names = sorted({v for v in (versions or statuses) if VERSION.fullmatch(v)}, key=lambda v: int(v[1:]))
    # a paper without any TeX version has 0 references, logged as 0 (statistics_m2 needs every paper)
    counts: Counter = Counter(rows_before_dedup=0)
    by_version, texts = {}, {}
    for v in names:
        g = gather(paper_dir / v, statuses.get(v, "missing"))
        counts["versions"] += 1
        if not g.ok:
            if version_log:
                version_log.write(time=now_iso(), arxiv_id=arxiv_id, version=v, gather_status=g.status)
            continue
        counts["versions_with_tex"] += 1
        bib_files = {rel: read_text(paper_dir / v / rel)[0] for rel in g.bib_files}
        bib_files.update(g.embedded_bib)
        entries = bibitem_parser.parse(g.text, bib_files)
        p = dict(bibitem_parser.LAST_STATS)
        rows = bibtex_converter.convert(entries)
        c = dict(bibtex_converter.LAST_STATS)
        if g.bib_fallback:
            counts["bib_fallback_versions"] += 1
            logging.getLogger("m2.references").info("%s: no bibliography declared, the folder's .bib files used", v)
        for name in ("bibitems", "amsrefs", "bib_entries_total", "bib_entries_used", "bib_uncited_dropped",
                     "cited_keys", "cited_not_found"):
            counts[name] += p.get(name, 0)
        counts["convert_failed"] += c.get("failed", 0)
        counts["rows_before_dedup"] += len(rows)
        counts["versions_with_refs"] += bool(rows)
        by_version[int(v[1:])] = rows
        texts[v] = g.text
        if version_log:
            version_log.write(time=now_iso(), arxiv_id=arxiv_id, version=v, gather_status=g.status,
                              bib_files=";".join(sorted(bib_files)), bib_fallback=int(g.bib_fallback),
                              has_thebibliography=int(g.has_thebibliography), rows=len(rows),
                              **{k: p.get(k, 0) for k in ("bibitems", "amsrefs", "bib_entries_total",
                                                           "bib_entries_used", "cited_keys", "cited_not_found")})
    rows, rename = dedup_bibtex.dedup(by_version) if by_version else ([], {})
    rename = citation_updater.extend_case_aliases(
        texts.values(), (row["citation_key"] for row in rows), rename)
    d = dict(dedup_bibtex.LAST_STATS)
    for text in texts.values():
        citation_updater.update(text, rename)
        counts["cite_commands_renamed"] += citation_updater.LAST_STATS.get("commands", 0)
    counts["rows_after_dedup"] = len(rows)
    counts["renamed_keys"] = len(rename)
    for name in ("key_conflicts", "joined_by_key", "joined_by_doi", "joined_by_arxiv", "joined_by_url",
                 "joined_by_title", "joined_by_fuzzy_title"):
        counts[name] = d.get(name, 0)
    for name in ("title", "authors", "doi", "journal", "url"):
        counts[f"with_{name}"] = sum(bool(r.get(name)) for r in rows)
    counts["with_year"] = sum(r.get("year") is not None for r in rows)
    counts["misc"] = sum(r.get("entry_type") == "misc" for r in rows)
    return dict(counts), rows, rename


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data", help="Milestone 1 data folder (corpus/, work/, logs/)")
    ap.add_argument("--ids", nargs="+", help="only these papers (2604.05535 or 2604-05535)")
    ap.add_argument("--test", action="store_true", help="only the first 5 papers")
    ap.add_argument("--limit", type=int, default=0, help="only the first N papers")
    ap.add_argument("--show", action="store_true", help="print the rows and renames of each paper")
    args = ap.parse_args(argv)

    from pipeline_m2 import list_papers
    layout = DataLayout(args.data_dir)
    setup_logging(layout.logs)
    papers = list_papers(layout, args.ids)
    if args.test:
        papers = papers[:5]
    elif args.limit:
        papers = papers[:args.limit]
    if not papers:
        print(f"no paper found in {layout.corpus}")
        return 1
    log = CsvLog(layout.logs / "reference_log.csv", LOG_FIELDS)
    version_log = CsvLog(layout.logs / "reference_versions.csv", VERSION_FIELDS)
    totals: Counter = Counter()
    started = time.perf_counter()
    for n, paper in enumerate(papers, 1):
        token = PAPER.set(paper["arxiv_id"])
        t0 = time.perf_counter()
        try:
            counts, rows, rename = references_of_paper(paper["arxiv_id"], Path(paper["paper_dir"]),
                                                       paper["versions"], version_log)
            status, error = "done", ""
        except Exception as exc:  # one paper never stops the run
            counts, rows, rename = {}, [], {}
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            logging.getLogger("m2.references").exception("paper failed")
        finally:
            PAPER.reset(token)
        seconds = round(time.perf_counter() - t0, 3)
        log.write(time=now_iso(), arxiv_id=paper["arxiv_id"], status=status, seconds=seconds, error=error,
                  **{k: counts.get(k, 0) for k in LOG_FIELDS if k in counts})
        totals.update({k: v for k, v in counts.items() if isinstance(v, int)})
        totals[status] += 1
        if args.show:
            print(f"== {paper['arxiv_id']}: {counts.get('rows_before_dedup', 0)} rows -> {len(rows)}, "
                  f"renames {json.dumps(rename, ensure_ascii=False)}")
            for r in rows:
                print("  " + json.dumps(r, ensure_ascii=False))
        if n % 200 == 0 or n == len(papers):
            print(f"[refs] {n}/{len(papers)} papers, {totals['rows_before_dedup']} rows -> "
                  f"{totals['rows_after_dedup']} after dedup, {time.perf_counter() - started:.0f}s", flush=True)

    before, after = totals["rows_before_dedup"], totals["rows_after_dedup"]
    print(f"papers: {totals['done']} done, {totals['failed']} failed | versions with references: "
          f"{totals['versions_with_refs']}/{totals['versions_with_tex']} with TeX")
    print(f"entries: {before} before dedup -> {after} after ({(1 - after / before) * 100 if before else 0:.1f}% "
          f"removed); {totals['renamed_keys']} keys renamed, {totals['key_conflicts']} key conflicts")
    if after:
        print("fields found: " + ", ".join(f"{k} {totals['with_' + k] / after:.0%}"
                                          for k in ("title", "authors", "year", "doi", "journal", "url"))
              + f"; misc {totals['misc'] / after:.0%}")
    print(f"logs: {layout.logs / 'reference_log.csv'}, reference_versions.csv, reference_m2.log")
    return 0 if not totals["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
