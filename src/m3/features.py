"""Milestone 3, feature engineering - one row per (entry, candidate reference) pair.

    python src/m3/features.py --work WORK [--labels WORK/labels.csv] [--out WORK/features.parquet]

Framing (see docs/m3_features.md): learning to rank. Every entry is a group;
its candidates are the references with an arXiv id of the same publication
(the m x n matrix of the statement); a model scores each pair and the top 5 of
the group are the prediction. Features are per pair, so the same table serves
a pairwise classifier (the baseline) and a ranker (the main model).

Rows: every candidate pair of every entry in labels.csv (merge_labels.py),
the is_match = false entries included: they are not samples (the lecturer: a
label is a matched pair) but the predictions table needs a ranking for them.
Columns:
  arxiv_id, citation_key, ref_arxiv_id   key (a candidates.parquet row)
  group                                  "arxiv_id|citation_key", one group per entry
  y                                      1 for the labelled reference, else 0
  is_sample                              1 when the entry is is_match = true
  source, range                          from labels.csv (manual / auto, ID range)
  <features>                             FEATURE_GROUPS below; NaN = unknown
FEATURE_GROUPS is also written to WORK/features.json.

Nothing is computed from the labels, the rule names or the hints of the
automatic labelling (no leakage); "context" features compare a candidate with
the other candidates of the same entry, which the model also has when it
predicts. External knowledge: none (no download, no pretrained model); the
TF-IDF weights come from the titles of the group's own corpus.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_label import GROUP_WORDS, NOT_NAMES, grams  # noqa: E402

FEATURE_GROUPS = {
    "link": ["id_match", "doi_match", "entry_has_id", "entry_has_doi", "entry_id_in_paper", "entry_doi_in_paper"],
    "title": ["title_jacc3", "title_jacc_word", "title_tfidf", "title_exact", "title_contain", "title_len_ratio",
              "entry_title_missing"],
    "author": ["first_author_match", "author_jacc", "author_overlap", "entry_authors_missing", "entry_authors_group"],
    "year": ["year_gap", "year_gap_signed", "year_missing"],
    "venue": ["venue_sim", "entry_is_preprint", "entry_type_code"],
    "context": ["n_candidates", "title_rank", "title_gap_best", "title_is_best", "title_margin_second",
                "tfidf_rank", "author_rank"],
}
FEATURES = [f for fs in FEATURE_GROUPS.values() for f in fs]
KEY_COLS = ["arxiv_id", "citation_key", "ref_arxiv_id", "group", "y", "is_sample", "source", "range"]
ENTRY_TYPES = ["article", "inproceedings", "misc", "book", "incollection", "phdthesis", "techreport", "inbook",
               "proceedings", "mastersthesis", "manual", "unpublished"]
NAN = float("nan")


def jaccard(a, b) -> float:
    return len(a & b) / len(a | b) if a and b else NAN


def idf_from_corpus(work: Path) -> dict[str, float]:
    """Inverse document frequency of the title words of every entry and every reference of the corpus."""
    df, docs = Counter(), 0
    for table, col in (("entries_clean.parquet", "title_tokens"), ("references_clean.parquet", "title_tokens")):
        for toks in pq.read_table(work / table, columns=[col]).column(col).to_pylist():
            if toks:
                df.update(set(toks))
                docs += 1
    return {t: math.log((1 + docs) / (1 + n)) + 1 for t, n in df.items()}


def tfidf(tokens, idf: dict[str, float]) -> dict[str, float]:
    if not tokens:
        return {}
    tf = Counter(tokens)
    v = {t: c * idf.get(t, 1.0) for t, c in tf.items()}
    norm = math.sqrt(sum(x * x for x in v.values()))
    return {t: x / norm for t, x in v.items()} if norm else {}


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return NAN
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def is_group_or_etal(names) -> bool:
    names = set(names or [])
    return bool(names) and (names <= NOT_NAMES or any(w in n.split() for n in names for w in GROUP_WORDS))


def prepare_ref(r: dict, idf) -> dict:
    t = r["title_clean"] or ""
    return {"id": r["ref_arxiv_id"], "title": t, "grams": grams(t) if t else frozenset(),
            "words": set(r["title_tokens"] or []), "tfidf": tfidf(r["title_tokens"], idf),
            "authors": set(r["authors_last"] or []) - NOT_NAMES,
            "years": [y for y in (r["year"], r["year_s2"]) if y],
            "dois": {d for d in (r["doi"], f"10.48550/arxiv.{r['ref_arxiv_id']}") if d},
            "venue": grams((r["venue"] or "").lower()) if r["venue"] else frozenset()}


def prepare_entry(e: dict, idf) -> dict:
    t = e["title_clean"] or ""
    journal = (e["journal"] or "").lower()
    return {"title": t, "grams": grams(t) if t else frozenset(), "words": set(e["title_tokens"] or []),
            "tfidf": tfidf(e["title_tokens"], idf), "authors": set(e["authors_last"] or []) - NOT_NAMES,
            "first": e["first_author_last"], "year": e["year"], "doi": e["doi"], "id": e["arxiv_id_in_entry"],
            "venue": grams(journal) if journal else frozenset(),
            "preprint": 1.0 if ("arxiv" in journal or "preprint" in journal or e["arxiv_id_from"]) else 0.0,
            "type": float(ENTRY_TYPES.index(e["entry_type"])) if e["entry_type"] in ENTRY_TYPES else float(len(ENTRY_TYPES)),
            "authors_missing": 0.0 if e["authors_last"] else 1.0,
            "authors_group": 1.0 if is_group_or_etal(e["authors_last"]) else 0.0}


def pair_features(e: dict, r: dict, paper_ids: set, paper_dois: set) -> dict:
    f = {}
    f["id_match"] = 1.0 if e["id"] and e["id"] == r["id"] else 0.0
    f["doi_match"] = 1.0 if e["doi"] and e["doi"] in r["dois"] else 0.0
    f["entry_has_id"] = 1.0 if e["id"] else 0.0
    f["entry_has_doi"] = 1.0 if e["doi"] else 0.0
    f["entry_id_in_paper"] = 1.0 if e["id"] and e["id"] in paper_ids else 0.0
    f["entry_doi_in_paper"] = 1.0 if e["doi"] and e["doi"] in paper_dois else 0.0
    has_title = bool(e["title"]) and bool(r["title"])
    f["title_jacc3"] = jaccard(e["grams"], r["grams"]) if has_title else NAN
    f["title_jacc_word"] = jaccard(e["words"], r["words"]) if has_title else NAN
    f["title_tfidf"] = cosine(e["tfidf"], r["tfidf"]) if has_title else NAN
    f["title_exact"] = (1.0 if e["title"] == r["title"] else 0.0) if has_title else NAN
    f["title_contain"] = (1.0 if (e["title"] in r["title"] or r["title"] in e["title"]) else 0.0) if has_title else NAN
    f["title_len_ratio"] = (min(len(e["title"]), len(r["title"])) / max(len(e["title"]), len(r["title"]))) \
        if has_title else NAN
    f["entry_title_missing"] = 0.0 if e["title"] else 1.0
    has_auth = bool(e["authors"]) and bool(r["authors"])
    f["first_author_match"] = (1.0 if e["first"] in r["authors"] else 0.0) if has_auth and e["first"] else NAN
    f["author_jacc"] = jaccard(e["authors"], r["authors"]) if has_auth else NAN
    f["author_overlap"] = float(len(e["authors"] & r["authors"])) if has_auth else NAN
    f["entry_authors_missing"] = e["authors_missing"]
    f["entry_authors_group"] = e["authors_group"]
    if e["year"] and r["years"]:
        signed = min((e["year"] - y for y in r["years"]), key=abs)
        f["year_gap"], f["year_gap_signed"], f["year_missing"] = float(abs(signed)), float(signed), 0.0
    else:
        f["year_gap"], f["year_gap_signed"], f["year_missing"] = NAN, NAN, 1.0
    f["venue_sim"] = jaccard(e["venue"], r["venue"]) if e["venue"] and r["venue"] else NAN
    f["entry_is_preprint"] = e["preprint"]
    f["entry_type_code"] = e["type"]
    return f


def ranks(values: list[float]) -> list[float]:
    """1 = best (largest); NaN ranks last; ties share the best rank."""
    order = sorted(((-v if v == v else math.inf) for v in values))
    return [float(order.index(-v if v == v else math.inf) + 1) for v in values]


def add_context(rows: list[dict]) -> None:
    n = len(rows)
    t = [r["title_jacc3"] for r in rows]
    known = sorted((x for x in t if x == x), reverse=True)
    best = known[0] if known else NAN
    second = known[1] if len(known) > 1 else NAN
    for r, rk, tr, ar in zip(rows, ranks(t), ranks([r["title_tfidf"] for r in rows]),
                             ranks([r["author_jacc"] for r in rows])):
        x = r["title_jacc3"]
        r["n_candidates"] = float(n)
        r["title_rank"], r["tfidf_rank"], r["author_rank"] = rk, tr, ar
        r["title_gap_best"] = best - x if x == x else NAN
        r["title_is_best"] = (1.0 if x == best else 0.0) if x == x else NAN
        r["title_margin_second"] = ((best - second) if second == second else best) if x == x and x == best else 0.0


def read_labels(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def build(work: Path, labels_path: Path, out: Path) -> dict:
    labels = read_labels(labels_path)
    by_paper = defaultdict(list)
    for r in labels:
        by_paper[r["arxiv_id"]].append(r)
    papers = set(by_paper)
    print(f"{len(labels)} entries of {len(papers)} publications; computing the TF-IDF weights", flush=True)
    idf = idf_from_corpus(work)
    ecols = ["arxiv_id", "citation_key", "entry_type", "journal", "title_clean", "title_tokens", "authors_last",
             "first_author_last", "year", "doi", "arxiv_id_in_entry", "arxiv_id_from"]
    entries = {(e["arxiv_id"], e["citation_key"]): e for e in
               pq.read_table(work / "entries_clean.parquet", columns=ecols).to_pylist() if e["arxiv_id"] in papers}
    refs = defaultdict(list)
    for r in pq.read_table(work / "references_clean.parquet",
                           columns=["arxiv_id", "ref_arxiv_id", "title_clean", "title_tokens", "authors_last",
                                    "year", "year_s2", "doi", "venue"]).to_pylist():
        if r["arxiv_id"] in papers:
            refs[r["arxiv_id"]].append(r)

    schema = pa.schema([(c, pa.int8() if c in ("y", "is_sample") else pa.string()) for c in KEY_COLS] +
                       [(f, pa.float32()) for f in FEATURES])
    tmp = out.with_suffix(".parquet.tmp")
    writer = pq.ParquetWriter(tmp, schema, compression="zstd")
    buf, total, groups, samples = [], 0, 0, 0

    def flush():
        nonlocal buf
        if buf:
            writer.write_table(pa.Table.from_pylist(buf, schema=schema))
            buf = []

    for i, pid in enumerate(sorted(papers)):
        prefs = [prepare_ref(r, idf) for r in sorted(refs.get(pid, []), key=lambda r: r["ref_arxiv_id"])]
        if not prefs:
            continue  # n = 0: no candidate, the predictions of these entries are empty
        ids = {r["id"] for r in prefs}
        dois = set().union(*(r["dois"] for r in prefs))
        for lab in by_paper[pid]:
            e = entries[(pid, lab["citation_key"])]
            pe = prepare_entry(e, idf)
            sample = 1 if lab["is_match"] == "true" else 0
            rows = []
            for r in prefs:
                f = pair_features(pe, r, ids, dois)
                f.update({"arxiv_id": pid, "citation_key": lab["citation_key"], "ref_arxiv_id": r["id"],
                          "group": f"{pid}|{lab['citation_key']}", "y": 1 if sample and r["id"] == lab["label_ref_arxiv_id"] else 0,
                          "is_sample": sample, "source": lab["source"], "range": lab.get("range", "")})
                rows.append(f)
            add_context(rows)
            buf += rows
            total += len(rows)
            groups += 1
            samples += sample
        if len(buf) >= 200_000:
            flush()
        if (i + 1) % 500 == 0:
            print(f"  {i + 1}/{len(papers)} publications, {total} pairs", flush=True)
    flush()
    writer.close()
    tmp.replace(out)
    meta = {"groups": FEATURE_GROUPS, "rows": total, "entries": groups, "samples": samples,
            "labels": str(labels_path.name)}
    (out.parent / "features.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"{total} pairs, {groups} entries ({samples} samples), {len(FEATURES)} features -> {out}")
    return meta


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--labels", type=Path, help="default WORK/labels.csv (merge_labels.py)")
    p.add_argument("--out", type=Path, help="default WORK/features.parquet")
    args = p.parse_args(argv)
    labels = args.labels or args.work / "labels.csv"
    if not labels.exists():  # pipeline_m3 runs every step present: no labels yet = nothing to do
        print(f"skipped: no {labels} (run merge_labels.py first)")
        return 0
    build(args.work, labels, args.out or args.work / "features.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
