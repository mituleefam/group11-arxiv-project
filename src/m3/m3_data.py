"""Milestone 3, Member 1 - load the two sides of the matching and clean them.

    python src/m3/m3_data.py --tables DIR_OF_M2_TABLES --data-dir M1_DATA_DIR [M1_DATA_DIR ...] --out WORK_DIR

Inputs
  --tables    the bibtex table of Milestone 2: a folder holding bibtex/*.parquet
              (the --out of pipeline_m2.py), the bibtex folder itself, or .parquet files
  --data-dir  one or more Milestone 1 data folders (the --data-dir of main.py),
              each holding corpus/<yymm-id>/references_all.json
Outputs, in WORK_DIR
  entries_clean.parquet      one row per BibTeX entry, key (arxiv_id, citation_key)
  references_clean.parquet   one row per crawled reference with an arXiv id,
                             key (arxiv_id, ref_arxiv_id)
  papers_refs.csv            per paper: Semantic Scholar status, references, arXiv references

The data are never changed: the cleaned columns are added next to the raw
ones, and what looks wrong is counted and printed, not repaired.

References come from references_all.json (--source all, the default): it
holds the year, the DOI and the venue of each reference, which the matching
uses, and the ids in dotted form like the bibtex table. --source subset reads
references.json instead (same references, ids in folder form 2511-23122, no
year, DOI or venue).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import text_clean as tc  # noqa: E402

ENTRY_SCHEMA = pa.schema([
    ("arxiv_id", pa.string()), ("citation_key", pa.string()),
    # raw columns of the bibtex table, kept for display (labelling tool) and checks
    ("entry_type", pa.string()), ("title", pa.string()), ("authors", pa.list_(pa.string())),
    ("journal", pa.string()), ("url", pa.string()),
    # cleaned columns
    ("title_clean", pa.string()), ("title_tokens", pa.list_(pa.string())),
    ("authors_last", pa.list_(pa.string())), ("first_author_last", pa.string()),
    ("year", pa.int32()), ("doi", pa.string()),
    ("arxiv_id_in_entry", pa.string()), ("arxiv_id_from", pa.string()),
])
REFERENCE_SCHEMA = pa.schema([
    ("arxiv_id", pa.string()), ("ref_arxiv_id", pa.string()),
    # raw columns, as in the references table of Milestone 1
    ("ref_title", pa.string()), ("ref_authors", pa.list_(pa.string())),
    ("ref_submission_date", pa.string()), ("ref_s2_id", pa.string()),
    # cleaned columns
    ("title_clean", pa.string()), ("title_tokens", pa.list_(pa.string())),
    ("authors_last", pa.list_(pa.string())), ("first_author_last", pa.string()),
    ("year", pa.int32()), ("year_s2", pa.int32()), ("doi", pa.string()), ("venue", pa.string()),
])
ARXIV_FIELDS = ("url", "journal", "doi", "title")  # where an entry may name its arXiv id, in this order
LONE_BRACE = re.compile(r"\{[^{}]?\}")  # "Ir {s}i {c}": what is left of \c{s} after Milestone 2


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def bibtex_files(paths) -> list[Path]:
    """The Parquet files of the bibtex table under each of `paths`."""
    files = []
    for p in map(Path, paths):
        if p.is_file():
            files.append(p)
            continue
        for folder in (p / "bibtex", p / "tables" / "bibtex", p):
            found = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
            if found:
                files += found
                break
        else:
            raise FileNotFoundError(f"no bibtex Parquet file under {p}")
    return files


def read_bibtex(paths) -> list[dict]:
    rows = []
    for f in bibtex_files(paths):
        rows += pq.read_table(f).to_pylist()
    for r in rows:  # dictionary columns come back as plain strings
        r["entry_type"] = None if r.get("entry_type") is None else str(r["entry_type"])
    return rows


def _paper_id(folder: Path, data: dict) -> str:
    return data.get("arxiv_id") or tc.normalize_arxiv_id(folder.name) or folder.name


def read_references(data_dirs, source: str = "all") -> tuple[list[dict], dict[str, dict]]:
    """(references with an arXiv id, per-paper status) of every paper folder
    under the Milestone 1 data folders."""
    refs, papers = [], {}
    for d in map(Path, data_dirs):
        corpus = d / "corpus" if (d / "corpus").is_dir() else d
        folders = sorted(p for p in corpus.iterdir() if p.is_dir())
        if not folders:
            raise FileNotFoundError(f"no paper folder under {corpus}")
        for folder in folders:
            all_path, sub_path = folder / "references_all.json", folder / "references.json"
            if source == "all" and all_path.is_file():
                data = json.loads(all_path.read_text(encoding="utf-8"))
                pid = _paper_id(folder, data)
                found = data.get("references") or []
                papers[pid] = {"arxiv_id": pid, "status": data.get("status"), "refs_total": len(found),
                               "refs_arxiv": 0, "source": "references_all.json"}
                for r in found:
                    if not r.get("ref_arxiv_id"):
                        continue
                    ext = r.get("ref_external_ids") or {}
                    refs.append({"arxiv_id": pid, "ref_arxiv_id": r["ref_arxiv_id"],
                                 "ref_title": r.get("ref_title"), "ref_authors": r.get("ref_authors") or [],
                                 "ref_submission_date": r.get("ref_submission_date"), "ref_s2_id": r.get("ref_s2_id"),
                                 "ref_year": r.get("ref_year"), "ref_doi": ext.get("DOI") if isinstance(ext, dict) else None,
                                 "ref_venue": r.get("ref_venue")})
                    papers[pid]["refs_arxiv"] += 1
            elif sub_path.is_file():
                data = json.loads(sub_path.read_text(encoding="utf-8"))
                pid = tc.normalize_arxiv_id(folder.name) or folder.name
                papers[pid] = {"arxiv_id": pid, "status": None, "refs_total": None, "refs_arxiv": len(data),
                               "source": "references.json"}
                for key, r in data.items():
                    refs.append({"arxiv_id": pid, "ref_arxiv_id": tc.normalize_arxiv_id(key),
                                 "ref_title": r.get("title"), "ref_authors": r.get("authors") or [],
                                 "ref_submission_date": r.get("submission_date"),
                                 "ref_s2_id": r.get("semantic_scholar_id"),
                                 "ref_year": None, "ref_doi": None, "ref_venue": None})
    return refs, papers


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def _names(authors_last: list[str]) -> tuple[list[str], str | None]:
    return authors_last, (authors_last[0] if authors_last else None)


def clean_entry(r: dict, remove_stop_words: bool = True) -> dict:
    keys, first = _names(tc.authors_last(r.get("authors")))
    arxiv, where = None, None
    for field in ARXIV_FIELDS:
        arxiv = tc.find_arxiv_id(r.get(field))
        if arxiv:
            where = field
            break
    return {"arxiv_id": r["arxiv_id"], "citation_key": r["citation_key"],
            "entry_type": r.get("entry_type"), "title": r.get("title"), "authors": list(r.get("authors") or []),
            "journal": r.get("journal"), "url": r.get("url"),
            "title_clean": tc.title_clean(r.get("title")),
            "title_tokens": tc.title_tokens(r.get("title"), remove_stop_words),
            "authors_last": keys, "first_author_last": first, "year": tc.year_of(r.get("year")),
            "doi": tc.normalize_doi(r.get("doi")) or tc.doi_from_url(r.get("url")),
            "arxiv_id_in_entry": arxiv, "arxiv_id_from": where}


def clean_reference(r: dict, remove_stop_words: bool = True) -> dict:
    keys, first = _names(tc.authors_last(r.get("ref_authors")))
    year_v1 = tc.year_of(r.get("ref_submission_date"))  # arXiv v1 date
    year_s2 = tc.year_of(r.get("ref_year"))             # Semantic Scholar's publication year
    return {"arxiv_id": r["arxiv_id"], "ref_arxiv_id": r["ref_arxiv_id"],
            "ref_title": r.get("ref_title"), "ref_authors": list(r.get("ref_authors") or []),
            "ref_submission_date": r.get("ref_submission_date"), "ref_s2_id": r.get("ref_s2_id"),
            "title_clean": tc.title_clean(r.get("ref_title")),
            "title_tokens": tc.title_tokens(r.get("ref_title"), remove_stop_words),
            "authors_last": keys, "first_author_last": first,
            "year": year_v1 if year_v1 is not None else year_s2, "year_s2": year_s2,
            "doi": tc.normalize_doi(r.get("ref_doi")), "venue": r.get("ref_venue")}


def write_table(rows: list[dict], schema: pa.Schema, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), tmp, compression="zstd")
    tmp.replace(path)


def load(work_dir) -> tuple[pa.Table, pa.Table]:
    """(entries_clean, references_clean) written by this script; .to_pandas() gives DataFrames."""
    work_dir = Path(work_dir)
    return (pq.read_table(work_dir / "entries_clean.parquet"),
            pq.read_table(work_dir / "references_clean.parquet"))


# ---------------------------------------------------------------------------
# Checks: counted and printed, nothing is repaired
# ---------------------------------------------------------------------------

def report(entries: list[dict], refs: list[dict], papers: dict[str, dict], raw_entries: list[dict]) -> list[str]:
    lines = []
    keys = Counter((e["arxiv_id"], e["citation_key"]) for e in entries)
    dup = sum(n - 1 for n in keys.values() if n > 1)
    rkeys = Counter((r["arxiv_id"], r["ref_arxiv_id"]) for r in refs)
    rdup = sum(n - 1 for n in rkeys.values() if n > 1)
    lines.append(f"entries {len(entries)} in {len({e['arxiv_id'] for e in entries})} papers; "
                 f"references with an arXiv id {len(refs)} in {len({r['arxiv_id'] for r in refs})} papers")
    if dup:
        lines.append(f"!! {dup} entries repeat a key (arxiv_id, citation_key) already seen")
    if rdup:
        lines.append(f"!! {rdup} references repeat a key (arxiv_id, ref_arxiv_id) already seen")
    changed = sum(1 for r in refs if tc.normalize_arxiv_id(r["ref_arxiv_id"]) != r["ref_arxiv_id"])
    if changed:
        lines.append(f"!! {changed} ref_arxiv_id are not in normalized form (version, upper case, subject class)")
    no_folder = {e["arxiv_id"] for e in entries} - set(papers)
    if no_folder:
        lines.append(f"!! {len(no_folder)} papers of the bibtex table have no Milestone 1 folder "
                     f"(wrong --data-dir?), e.g. {sorted(no_folder)[:3]}")
    status = Counter(p["status"] for p in papers.values())
    lines.append(f"Semantic Scholar status of the papers: {dict(status)}")
    with_refs = {r["arxiv_id"] for r in refs}
    lines.append(f"papers with entries but no arXiv reference (every entry is 'no match'): "
                 f"{len({e['arxiv_id'] for e in entries} - with_refs)}")
    n = len(entries) or 1
    lines.append(f"entries: no title {sum(1 for e in entries if not e['title_clean'])} "
                 f"({100 * sum(1 for e in entries if not e['title_clean']) / n:.1f}%), "
                 f"no author {sum(1 for e in entries if not e['authors_last'])}, no year "
                 f"{sum(1 for e in entries if e['year'] is None)}, DOI {sum(1 for e in entries if e['doi'])}, "
                 f"arXiv id {sum(1 for e in entries if e['arxiv_id_in_entry'])} "
                 f"({100 * sum(1 for e in entries if e['arxiv_id_in_entry']) / n:.1f}%, first found in "
                 f"{dict(Counter(e['arxiv_id_from'] for e in entries if e['arxiv_id_from']))})")
    # raw values that the cleaning had to work around (Milestone 2 output, reported only)
    names = [a for e in raw_entries for a in (e.get("authors") or []) if a]
    trailing = sum(1 for a in names if a.rstrip().endswith("\\"))
    several = sum(1 for a in names if len(tc.split_authors([a])) > 1)
    broken = sum(1 for a in names if LONE_BRACE.search(a))
    if trailing:
        lines.append(f"!! bibtex: {trailing} author names end with a backslash ('Yao, D.-X. \\\\')")
    if several:
        lines.append(f"!! bibtex: {several} author strings hold several people ('E. Ozkan, H. Aydin, R. Dikici'), split here")
    if broken:
        lines.append(f"!! bibtex: {broken} author names hold a lone braced letter ('Ir {{s}}i {{c}}, V.', an accent lost by Milestone 2)")
    others = sum(1 for a in names if tc.normalize_text(a) in tc.NOT_A_PERSON)
    if others:
        lines.append(f"bibtex: {others} author names are 'others' / 'et al.' (dropped from authors_last)")
    rn = len(refs) or 1
    lines.append(f"references: DOI {sum(1 for r in refs if r['doi'])} ({100 * sum(1 for r in refs if r['doi']) / rn:.1f}%, "
                 f"of which arXiv DOI {sum(1 for r in refs if (r['doi'] or '').startswith('10.48550/'))}), "
                 f"year differs between arXiv v1 and Semantic Scholar "
                 f"{sum(1 for r in refs if r['year'] is not None and r['year_s2'] is not None and r['year'] != r['year_s2'])}")
    return lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load and clean the bibtex entries and the crawled references")
    p.add_argument("--tables", nargs="+", required=True, help="Milestone 2 tables folder, bibtex folder or .parquet files")
    p.add_argument("--data-dir", nargs="+", required=True, help="Milestone 1 data folders (holding corpus/)")
    p.add_argument("--out", type=Path, required=True, help="work folder of Milestone 3")
    p.add_argument("--source", choices=("all", "subset"), default="all",
                   help="references_all.json (default) or references.json")
    p.add_argument("--keep-stop-words", action="store_true", help="keep stop words in title_tokens")
    args = p.parse_args(argv)

    raw_entries = read_bibtex(args.tables)
    raw_refs, papers = read_references(args.data_dir, args.source)
    stop = not args.keep_stop_words
    entries = [clean_entry(r, stop) for r in raw_entries]
    refs = [clean_reference(r, stop) for r in raw_refs]
    write_table(entries, ENTRY_SCHEMA, args.out / "entries_clean.parquet")
    write_table(refs, REFERENCE_SCHEMA, args.out / "references_clean.parquet")
    with open(args.out / "papers_refs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["arxiv_id", "status", "refs_total", "refs_arxiv", "source"])
        w.writeheader()
        w.writerows(sorted(papers.values(), key=lambda x: x["arxiv_id"]))
    for line in report(entries, refs, papers, raw_entries):
        print(line)
    print(f"written: {args.out / 'entries_clean.parquet'}, {args.out / 'references_clean.parquet'}, "
          f"{args.out / 'papers_refs.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
