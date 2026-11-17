"""Milestone 3, labelling step 1 - choose the publications to label by hand.

    python src/m3/select_manual.py --work WORK --data-dir M1_DIR [M1_DIR ...]
                                   [--per-range 5] [--seed 11] [--exclude FILE] [--out WORK/manual_pick.csv]

The statement asks each member for at least 5 manually labelled publications
with at least 20 labels each. The lecturer's answers (Q&A) make precise that a
label is a matched (entry, reference) pair: an entry with no reference among
the publication's crawled references is not a label, does not count towards
the 20 and is not a sample of MRR@5. A publication can therefore reach 20
labels only if at least 20 of its references (with an arXiv id) are cited by
one of its BibTeX entries. The choice is made per range (each member hosts the
data of their own range); the range of a publication comes from its arXiv id
(GROUP_RANGES below).

Exact rules (the simplest automatic matching): an entry names a reference of
the same publication by the same arXiv id, the same DOI (the arXiv DOI
10.48550/arxiv.<ref id> included) or an identical title_clean.
  rule_matched      entries the exact rules match
  sure_pairs        distinct references they reach: a lower bound of the
                    labels the publication will give (the person usually
                    finds more, the pairs the rules miss)
  rule_share        rule_matched / m: share of the entries the rules settle;
                    the rest is what the person must judge (a pair the rules
                    miss, or no reference at all). On the GBao range the rules
                    already reach almost every arXiv reference of an eligible
                    publication (sure_pairs / n has median 1.0, 10th percentile
                    0.93), so sure_pairs / n hardly tells publications apart,
                    while rule_share spreads from 0.24 to 0.86.

Eligible publication (thresholds are flags):
  - Semantic Scholar status "success" (papers_refs.csv);
  - --min-m <= m <= --max-m BibTeX entries (default 25..100);
  - n >= --min-n references with an arXiv id (default 25): room for the 20 pairs;
  - sure_pairs >= --min-sure (default 10): half of the 20 pairs already certain;
  - at most --max-no-title entries without a title, at most --max-misc of misc entries.

Difficulty is relative inside the range: the eligible publications are cut in
three equal parts by rule_share; easy = the third the rules match best, hard =
the third they match worst. Hard publications are kept on purpose: with easy
ones only, the manual labels repeat the automatic rules and the test MRR looks
better than it is. Per range the levels are taken in the order medium, hard,
easy, medium, hard (5 publications: 1 easy, 2 medium, 2 hard), each drawn at
random with a fixed seed inside its level; then, when every pick is a cs paper
and the range has an eligible paper of another field, the last pick is swapped
for one of the same level outside cs, so the model is not tested on cs only.

Writes manual_pick.csv: range, arxiv_id, primary_category, m, n, rule_matched,
sure_pairs, rule_share, difficulty, reason.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

GROUP_RANGES = {  # name -> (first id, last id), the group's assignment
    "Tram": ("2511.21709", "2512.03230"),
    "Thu": ("2512.13231", "2512.18230"),
    "Phat": ("2601.13156", "2601.18155"),
    "Bao": ("2604.05535", "2604.10534"),
    "Quan": ("2605.12338", "2605.17337"),
}
LEVEL_ORDER = ("medium", "hard", "easy")
FIELDS = ["range", "arxiv_id", "primary_category", "m", "n", "rule_matched", "sure_pairs", "rule_share",
          "difficulty", "reason"]


def id_key(arxiv_id: str) -> tuple[int, int]:
    """2604.05535 -> (2604, 5535): ids compare as numbers (4 and 5 digit numbers)."""
    yymm, number = arxiv_id.split(".")
    return int(yymm), int(number)


def range_of(arxiv_id: str) -> str | None:
    if "/" in arxiv_id or "." not in arxiv_id:
        return None
    k = id_key(arxiv_id)
    for name, (first, last) in GROUP_RANGES.items():
        if id_key(first) <= k <= id_key(last):
            return name
    return None


def exact_match(entry: dict, ids: set, dois: dict, titles: dict) -> str | None:
    """ref_arxiv_id an exact rule gives the entry, else None. dois/titles map a value to its reference."""
    if entry["arxiv_id_in_entry"] and entry["arxiv_id_in_entry"] in ids:
        return entry["arxiv_id_in_entry"]
    if entry["doi"] and entry["doi"] in dois:
        return dois[entry["doi"]]
    if entry["title_clean"] and entry["title_clean"] in titles:
        return titles[entry["title_clean"]]
    return None


def rule_matches(work: Path) -> tuple[Counter, Counter, dict[str, set]]:
    """Per publication: entries, entries matched by the exact rules, distinct references they reach."""
    refs = defaultdict(lambda: (set(), {}, {}))
    for r in pq.read_table(work / "references_clean.parquet",
                           columns=["arxiv_id", "ref_arxiv_id", "doi", "title_clean"]).to_pylist():
        ids, dois, titles = refs[r["arxiv_id"]]
        rid = r["ref_arxiv_id"]
        ids.add(rid)
        for d in (r["doi"], f"10.48550/arxiv.{rid}"):
            if d:
                dois.setdefault(d, rid)
        if r["title_clean"]:
            titles.setdefault(r["title_clean"], rid)
    total, hit, reached = Counter(), Counter(), defaultdict(set)
    for e in pq.read_table(work / "entries_clean.parquet",
                           columns=["arxiv_id", "arxiv_id_in_entry", "doi", "title_clean"]).to_pylist():
        pid = e["arxiv_id"]
        total[pid] += 1
        if pid in refs:
            rid = exact_match(e, *refs[pid])
            if rid:
                hit[pid] += 1
                reached[pid].add(rid)
    return total, hit, reached


def categories(data_dirs: list[Path]) -> dict[str, str]:
    """arxiv_id -> arXiv primary category, from metadata.json of the M1 folders."""
    out = {}
    for d in data_dirs:
        for folder in (d / "corpus").iterdir():
            try:
                meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out[folder.name.replace("-", ".", 1)] = meta.get("primary_category") or ""
    return out


def eligible(work: Path, cats: dict[str, str], args, exclude: set[str]) -> list[dict]:
    with open(work / "paper_stats.csv", encoding="utf-8", newline="") as f:
        stats = list(csv.DictReader(f))
    with open(work / "papers_refs.csv", encoding="utf-8", newline="") as f:
        status = {s["arxiv_id"]: s["status"] for s in csv.DictReader(f)}
    total, hit, reached = rule_matches(work)
    out = []
    for s in stats:
        pid = s["arxiv_id"]
        m, n = int(s["m"]), int(s["n"])
        if pid in exclude or status.get(pid) != "success" or range_of(pid) is None:
            continue
        if not (args.min_m <= m <= args.max_m and n >= args.min_n and len(reached[pid]) >= args.min_sure):
            continue
        if int(s["entries_without_title"]) > args.max_no_title or int(s["misc_entries"]) > args.max_misc * m:
            continue
        out.append({"range": range_of(pid), "arxiv_id": pid, "primary_category": cats.get(pid, ""),
                    "m": m, "n": n, "rule_matched": hit[pid], "sure_pairs": len(reached[pid]),
                    "rule_share": round(hit[pid] / m, 3)})
    return out


def set_difficulty(papers: list[dict]) -> None:
    """Thirds by rule_share inside the group of papers: easy (best matched), medium, hard."""
    ranked = sorted(papers, key=lambda p: (-p["rule_share"], id_key(p["arxiv_id"])))
    for i, p in enumerate(ranked):
        p["difficulty"] = ("easy", "medium", "hard")[min(2, 3 * i // max(1, len(ranked)))]


def is_cs(p: dict) -> bool:
    return p["primary_category"].startswith("cs.")


def pick(papers: list[dict], k: int, rng: random.Random) -> list[dict]:
    """k publications of one range over the difficulty levels, at least one outside cs when possible."""
    levels = defaultdict(list)
    for p in sorted(papers, key=lambda p: id_key(p["arxiv_id"])):
        levels[p["difficulty"]].append(p)
    for v in levels.values():
        rng.shuffle(v)
    chosen = []
    for i in range(min(k, len(papers))):
        for step in range(len(LEVEL_ORDER)):  # the level of this turn, else the next one with something left
            level = LEVEL_ORDER[(i + step) % len(LEVEL_ORDER)]
            if levels[level]:
                p = dict(levels[level].pop())
                p["reason"] = f"{level} (rule_share {p['rule_share']:.2f})" + \
                              ("" if step == 0 else f", no {LEVEL_ORDER[i % len(LEVEL_ORDER)]} paper left")
                chosen.append(p)
                break
    if chosen and all(is_cs(p) for p in chosen):
        last = chosen[-1]
        same = [p for p in levels[last["difficulty"]] if not is_cs(p)]
        other = same or [p for v in levels.values() for p in v if not is_cs(p)]
        if other:
            p = dict(other[0])
            p["reason"] = f"{p['difficulty']} (rule_share {p['rule_share']:.2f}), outside cs (replaces {last['arxiv_id']})"
            chosen[-1] = p
    return sorted(chosen, key=lambda p: id_key(p["arxiv_id"]))


def read_exclude(path: Path | None) -> set[str]:
    if not path:
        return set()
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".csv":
        return {r["arxiv_id"] for r in csv.DictReader(text.splitlines())}
    return {line.strip() for line in text.splitlines() if line.strip()}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True, help="M3 work folder (m3_data.py + candidates.py output)")
    p.add_argument("--data-dir", type=Path, nargs="+", required=True, help="M1 folders (corpus/<id>/metadata.json)")
    p.add_argument("--per-range", type=int, default=5, help="publications per range (default 5)")
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--exclude", type=Path, help="papers not to pick: a .csv with arxiv_id, or one id per line")
    p.add_argument("--min-m", type=int, default=25)
    p.add_argument("--max-m", type=int, default=100)
    p.add_argument("--min-n", type=int, default=25)
    p.add_argument("--min-sure", type=int, default=10, help="least distinct references the exact rules reach")
    p.add_argument("--max-no-title", type=int, default=2)
    p.add_argument("--max-misc", type=float, default=0.3, help="largest share of misc entries")
    p.add_argument("--out", type=Path, help="default WORK/manual_pick.csv")
    args = p.parse_args(argv)

    papers = eligible(args.work, categories(args.data_dir), args, read_exclude(args.exclude))
    by_range = defaultdict(list)
    for x in papers:
        by_range[x["range"]].append(x)
    rows = []
    for name in GROUP_RANGES:
        if name not in by_range:
            continue
        group = by_range[name]
        set_difficulty(group)
        chosen = pick(group, args.per_range, random.Random(f"{args.seed}:{name}"))
        cs = sum(is_cs(x) for x in group)
        cuts = {lvl: [x["rule_share"] for x in group if x["difficulty"] == lvl] for lvl in ("easy", "medium", "hard")}
        print(f"{name}: {len(group)} eligible (cs {cs}, other {len(group) - cs}); rule_share easy "
              f">= {min(cuts['easy'], default=0):.2f}, hard <= {max(cuts['hard'], default=0):.2f} -> {len(chosen)} picked")
        if len(chosen) < args.per_range:
            print(f"  !! only {len(chosen)} eligible publications")
        for c in chosen:
            print(f"  {c['arxiv_id']}  {c['primary_category']:<18} m={c['m']:<3} n={c['n']:<3} "
                  f"rule={c['rule_matched']:<3} sure_pairs={c['sure_pairs']:<3} {c['reason']}")
        rows += chosen
    out = args.out or args.work / "manual_pick.csv"
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} publications, {sum(r['m'] for r in rows)} entries to look at, "
          f"{sum(r['sure_pairs'] for r in rows)} pairs already sure, at most {sum(r['n'] for r in rows)} pairs -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
