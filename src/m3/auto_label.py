"""Milestone 3, labelling step 3 - automatic labels by fixed matching rules.

    python src/m3/auto_label.py --work WORK [--manual WORK/manual_pick.csv] [--fraction 0.10] [--seed 11]
                                [--out WORK/labels_auto.csv]
    python src/m3/auto_label.py --work WORK --evaluate WORK/labels_manual_*.csv [--errors FILE.csv]

The statement: "Implement automatic matching with classic tools such as regex
or string similarity heuristics and label at least 10% of the publications
outside the manual pool. The automatic matcher labels every BibTeX entry of the
publications it is applied to." The lecturer (Q&A): the matcher is an
algorithm fixed in advance, not the trained model; 10% counts publications, per
member (about 500 of 5000); a publication labelled by hand never goes through
the automatic labelling; a label is a matched (entry, reference) pair.

Pool, per ID range (select_manual.GROUP_RANGES): the publications of the range
outside the manual picks, drawn at random with a fixed seed among those with
Semantic Scholar status "success", m >= 1 and n >= --min-n (default 5: no
label can come from a publication without references, and a publication that
goes to test or validation needs candidates to rank). Size:
ceil(--fraction * (publications of the range - manual picks of the range)).

Rules, inside one publication, every entry against every reference:
  R1 same arXiv id     arxiv_id_in_entry == ref_arxiv_id
  R2 same DOI          entry doi == reference doi, or == 10.48550/arxiv.<ref id>
  R3 same title        title_clean equal to the title of exactly one reference,
                       at least 3 words, and either years at most 1 apart (when
                       both known; the reference has two: arXiv v1 and Semantic
                       Scholar) or the authors agree (a journal version cited
                       years after the preprint: 5 of the 168 manual labels of
                       the GBao range are such pairs, 2 to 3 years apart);
                       a title of <= SHORT_TITLE (5) words is refused when both
                       sides name authors and they do not agree ("Generative
                       adversarial networks" by Goodfellow is not the 2022
                       review of the same title by Cohen and Giryes)
  R4 close title       title similarity >= --fuzzy (0.85), with --sim chars
                       (default): Jaccard of the character 3-grams of the title
                       without spaces, so "M 2 diffuser" = "M2Diffuser" and a
                       changed word costs little; --sim words: difflib ratio of
                       the sorted words (the first version, threshold 0.93);
                       at least --margin (0.10) above the second best reference,
                       same first author surname or half of the surnames shared,
                       years at most 1 apart when both known
R1-R3 are exact; when they name different references the entry is left
without label ("conflict"). R4 is tried only when R1-R3 found nothing.

An id or DOI written by the author can be wrong (pasted from another paper),
so an R1/R2 label is checked against the title ("id_mismatch", no label):
  - always: the entry has a full title (>= 4 words, not a fragment such as
    "Phys" or "Arxiv: 2310.10746" left by a garbled entry), its similarity with
    the reference title is < MISMATCH (0.15) and the authors do not agree;
  - when another entry of the publication reaches the same reference by its
    title (similarity >= --fuzzy): the R1/R2 entry has a full title of
    similarity < MISMATCH whatever the authors (a wrongly pasted id usually
    belongs to a paper of the same group), or < MISMATCH_OTHER_AUTHORS (0.30)
    when the authors do not agree (every renamed paper seen kept its authors).
MISMATCH is low on purpose: papers renamed between arXiv versions keep their
id with a title of similarity 0.16 to 1.0 (MonST3R 0.16, Emergent
Misalignment 0.21, MathVista 0.41), wrongly pasted ids gave 0.07 to 0.21.
Several entries left with the same reference are all kept and marked
"shared": in 92 of 102 such cases checked, the same work was cited under two
keys (twice in the .bib, preprint and published version, or one entry garbled
by M2), so every one of them is a label.

Output: labels_auto.csv, one row per entry of the pool publications, in the
format of the manual labels (label_sheet.py import):
    arxiv_id, citation_key, label_ref_arxiv_id, is_match, source=auto, labeler=auto, note
note = the rule (R1..R4), "conflict ...", "id_mismatch ..." or "... shared ...". Only is_match = true
rows are labels.

--evaluate: runs the same rules on the publications of manual label files and
compares with the manual labels: precision and recall overall and per rule,
and the list of differences (--errors). Nothing is written as labels.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from select_manual import GROUP_RANGES, id_key, range_of  # noqa: E402

LABEL_COLS = ["arxiv_id", "citation_key", "label_ref_arxiv_id", "is_match", "source", "labeler", "note"]
STRONG = ("R1", "R2")
MISMATCH = 0.15  # title similarity under which an id / DOI label is doubted (see id_mismatch)
SHORT_TITLE = 5  # R3: a title this short is shared by other works; the authors must not conflict
MISMATCH_OTHER_AUTHORS = 0.30  # the same, when another entry has the reference and the authors differ
ENTRY_COLS = ["arxiv_id", "citation_key", "title_clean", "authors_last", "first_author_last", "year", "doi",
              "arxiv_id_in_entry"]
REF_COLS = ["arxiv_id", "ref_arxiv_id", "title_clean", "authors_last", "year", "year_s2", "doi"]


def words(title: str | None) -> str:
    return " ".join(sorted((title or "").split()))


def grams(title: str | None, n: int = 3) -> frozenset:
    """Character n-grams of the title without spaces: "M 2 diffuser" and
    "M2Diffuser" give the same grams, a changed word only changes a few."""
    s = (title or "").replace(" ", "")
    return frozenset(s[i:i + n] for i in range(len(s) - n + 1)) if len(s) >= n else frozenset({s})


def similarity(kind: str, a, b) -> float:
    if kind == "chars":  # Jaccard of character 3-grams
        return len(a & b) / len(a | b) if a and b else 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()  # "words": ratio of the sorted words


def years_close(year, ref: dict, gap: int = 1) -> bool:
    years = [y for y in (ref["year"], ref["year_s2"]) if y]
    return not year or not years or min(abs(year - y) for y in years) <= gap


NOT_NAMES = {"al", "others", "anonymous"}  # what is left of "et al." or a missing author list


GROUP_WORDS = ("team", "openai", "google", "deepmind", "meta", "anthropic", "collaboration", "consortium",
               "project", "group", "lab", "inc", "corporation", "university")


def authors_conflict(entry: dict, ref: dict) -> bool:
    """Both sides name real persons and no surname agrees, not even as a
    transliteration (shanskii / shansky). An organisation as author ("Team
    Qwen", "OpenAI") never conflicts: Semantic Scholar lists the persons."""
    a = set(entry["authors_last"] or []) - NOT_NAMES
    b = set(ref["authors_last"] or []) - NOT_NAMES
    if not a or not b or authors_agree(entry, ref):
        return False
    if any(w in name.split() for name in a | b for w in GROUP_WORDS):
        return False
    return not any(difflib.SequenceMatcher(None, x, y).ratio() >= 0.8 for x in a for y in b)


def authors_agree(entry: dict, ref: dict) -> bool:
    a, b = set(entry["authors_last"] or []), set(ref["authors_last"] or [])
    if not a or not b:
        return False
    return (entry["first_author_last"] in b) or len(a & b) / min(len(a), len(b)) >= 0.5


class Paper:
    """The references of one publication, indexed for the rules."""

    def __init__(self, refs: list[dict]):
        self.refs = refs
        self.ids = {r["ref_arxiv_id"] for r in refs}
        self.doi = defaultdict(set)
        self.title = defaultdict(list)
        for r in refs:
            for d in (r["doi"], f"10.48550/arxiv.{r['ref_arxiv_id']}"):
                if d:
                    self.doi[d].add(r["ref_arxiv_id"])
            if r["title_clean"]:
                self.title[r["title_clean"]].append(r)
        self.words = {r["ref_arxiv_id"]: words(r["title_clean"]) for r in refs}
        self.grams = {r["ref_arxiv_id"]: grams(r["title_clean"]) for r in refs}

    def exact(self, e: dict) -> dict[str, str]:
        found = {}
        if e["arxiv_id_in_entry"] in self.ids:
            found["R1"] = e["arxiv_id_in_entry"]
        if e["doi"] and len(self.doi.get(e["doi"], ())) == 1:
            found["R2"] = next(iter(self.doi[e["doi"]]))
        same = self.title.get(e["title_clean"] or "", [])
        n_words = len((e["title_clean"] or "").split())
        if len(same) == 1 and n_words >= 3 and (years_close(e["year"], same[0]) or authors_agree(e, same[0]))                 and not (n_words <= SHORT_TITLE and authors_conflict(e, same[0])):
            found["R3"] = same[0]["ref_arxiv_id"]
        return found

    def fuzzy(self, e: dict, threshold: float, margin: float, kind: str = "chars") -> str | None:
        if not e["title_clean"] or len(e["title_clean"].split()) < 3:
            return None
        mine, theirs = (grams(e["title_clean"]), self.grams) if kind == "chars" else (words(e["title_clean"]), self.words)
        scored = sorted(((similarity(kind, mine, theirs[r["ref_arxiv_id"]]), r)
                         for r in self.refs if r["title_clean"]), key=lambda x: -x[0])
        if not scored or scored[0][0] < threshold:
            return None
        if len(scored) > 1 and scored[0][0] - scored[1][0] < margin:
            return None
        best = scored[0][1]
        if not authors_agree(e, best) or not years_close(e["year"], best):
            return None
        return best["ref_arxiv_id"]


def full_title(title: str | None) -> bool:
    """A real title, not a fragment M2 kept from a garbled entry: "Phys", "arxiv 2310 10746",
    a journal reference ("phys a 20 3765 hep ex 0409052", "chaos solitons fractals 202") or
    author initials ("m e j newman", "he and x d ji")."""
    w = (title or "").split()
    if len(w) < 4 or "arxiv" in w:
        return False
    if any(x.isdigit() for x in w) and len(w) <= 8:  # volume / page numbers
        return False
    return 2 * sum(len(x) <= 2 for x in w) < len(w)  # mostly initials


def title_similarity(entry: dict, ref: dict) -> float:
    return similarity("chars", grams(entry["title_clean"]), grams(ref["title_clean"]))


def label_paper(entries: list[dict], paper: Paper, threshold: float, margin: float,
                kind: str = "chars", mismatch: float = MISMATCH) -> list[dict]:
    refs = {r["ref_arxiv_id"]: r for r in paper.refs}
    by_key = {e["citation_key"]: e for e in entries}
    out = []
    for e in entries:
        found = paper.exact(e)
        rid, note = None, ""
        if len(set(found.values())) > 1:
            note = "conflict " + ", ".join(f"{k}->{v}" for k, v in sorted(found.items()))
        elif found:
            rule = min(found)  # R1 before R2 before R3
            rid, note = found[rule], rule
            if rule in STRONG and full_title(e["title_clean"]) and refs[rid]["title_clean"] and (
                    title_similarity(e, refs[rid]) < mismatch and not authors_agree(e, refs[rid])):
                rid, note = None, f"id_mismatch {rule}->{found[rule]}: other title, other authors"
        else:
            rid = paper.fuzzy(e, threshold, margin, kind)
            note = "R4" if rid else ""
        out.append({"arxiv_id": e["arxiv_id"], "citation_key": e["citation_key"], "label_ref_arxiv_id": rid or "",
                    "note": note})
    by_ref = defaultdict(list)
    for x in out:
        if x["label_ref_arxiv_id"]:
            by_ref[x["label_ref_arxiv_id"]].append(x)
    for rid, xs in by_ref.items():
        if len(xs) < 2:
            continue
        ref = refs[rid]
        weak_close = [x for x in xs if x["note"] not in STRONG
                      and title_similarity(by_key[x["citation_key"]], ref) >= threshold]
        for x in xs:  # an id / DOI label whose own title is another paper, while a title match is this one
            e = by_key[x["citation_key"]]
            sim = title_similarity(e, ref)
            if x["note"] in STRONG and weak_close and full_title(e["title_clean"]) and (
                    sim < mismatch or (sim < MISMATCH_OTHER_AUTHORS and not authors_agree(e, ref))):
                x["note"] = (f"id_mismatch {x['note']}->{rid}: title of another paper, {rid} is "
                             f"{weak_close[0]['citation_key']}")
                x["label_ref_arxiv_id"] = ""
        kept = [x for x in xs if x["label_ref_arxiv_id"]]
        if len(kept) > 1:  # the same work cited under several keys: every one is a label
            for x in kept:
                x["note"] += f" shared {rid}"
    for x in out:
        x["is_match"] = "true" if x["label_ref_arxiv_id"] else "false"
        x["source"], x["labeler"] = "auto", "auto"
    return out


def load(work: Path, papers: set[str] | None):
    entries, refs = defaultdict(list), defaultdict(list)
    for e in pq.read_table(work / "entries_clean.parquet", columns=ENTRY_COLS).to_pylist():
        if papers is None or e["arxiv_id"] in papers:
            entries[e["arxiv_id"]].append(e)
    for r in pq.read_table(work / "references_clean.parquet", columns=REF_COLS).to_pylist():
        if papers is None or r["arxiv_id"] in papers:
            refs[r["arxiv_id"]].append(r)
    return entries, refs


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def choose_pool(work: Path, manual: set[str], fraction: float, min_n: int, seed: int) -> dict[str, list[str]]:
    status = {r["arxiv_id"]: r["status"] for r in read_csv(work / "papers_refs.csv")}
    stats = {r["arxiv_id"]: r for r in read_csv(work / "paper_stats.csv")}
    pool = {}
    for name in GROUP_RANGES:
        papers = sorted((p for p in status if range_of(p) == name), key=id_key)
        if not papers:
            continue
        outside = [p for p in papers if p not in manual]
        want = math.ceil(fraction * len(outside))
        ok = [p for p in outside if status[p] == "success" and p in stats
              and int(stats[p]["m"]) >= 1 and int(stats[p]["n"]) >= min_n]
        rng = random.Random(f"{seed}:auto:{name}")
        chosen = rng.sample(ok, min(want, len(ok)))
        if len(chosen) < want:
            print(f"!! {name}: only {len(chosen)} eligible publications for {want} wanted")
        pool[name] = sorted(chosen, key=id_key)
        print(f"{name}: {len(papers)} publications, {len(outside)} outside the manual pool, "
              f"{len(ok)} eligible -> {len(chosen)} drawn ({fraction:.0%})")
    return pool


def cmd_label(args) -> int:
    manual = {r["arxiv_id"] for r in read_csv(args.manual)} if args.manual and args.manual.exists() else set()
    if not manual:
        print("!! no manual picks given: nothing is excluded from the automatic pool")
    pool = choose_pool(args.work, manual, args.fraction, args.min_n, args.seed)
    chosen = {p for ps in pool.values() for p in ps}
    entries, refs = load(args.work, chosen)
    rows = []
    for name, papers in pool.items():
        part = []
        for p in papers:
            part += label_paper(sorted(entries[p], key=lambda e: e["citation_key"]), Paper(refs[p]),
                                args.fuzzy, args.margin, args.sim)
        rules = Counter(x["note"].split()[0] for x in part if x["is_match"] == "true")
        conflicts = sum(1 for x in part if x["note"].startswith("conflict"))
        matched = sum(1 for x in part if x["is_match"] == "true")
        print(f"  {name}: {len(papers)} publications, {len(part)} entries, {matched} labels "
              f"({matched / max(1, len(part)):.1%}) {dict(sorted(rules.items()))}, {conflicts} conflicts left out")
        rows += part
    out = args.out or args.work / "labels_auto.csv"
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"{len(chosen)} publications, {len(rows)} entries, "
          f"{sum(1 for x in rows if x['is_match'] == 'true')} labels -> {out}")
    return 0


def cmd_evaluate(args) -> int:
    manual = [r for path in args.evaluate for r in read_csv(path) if (r.get("source") or "manual") == "manual"]
    truth = {(r["arxiv_id"], r["citation_key"]): r["label_ref_arxiv_id"].strip() for r in manual}
    papers = {p for p, _ in truth}
    entries, refs = load(args.work, papers)
    pred = {}
    for p in sorted(papers, key=id_key):
        for x in label_paper(entries[p], Paper(refs[p]), args.fuzzy, args.margin, args.sim):
            pred[(x["arxiv_id"], x["citation_key"])] = x
    missing = [k for k in truth if k not in pred]
    if missing:
        print(f"!! {len(missing)} labelled entries are not in the work folder, e.g. {missing[:3]}")
    per_rule, errors = defaultdict(Counter), []
    tp = fp = fn = 0
    for k, gold in truth.items():
        x = pred.get(k)
        if x is None:
            continue
        got, rule = x["label_ref_arxiv_id"], (x["note"].split()[0] if x["label_ref_arxiv_id"] else "")
        if got:
            ok = got == gold
            per_rule[rule]["correct" if ok else "wrong"] += 1
            tp += ok
            fp += not ok
            if not ok:
                errors.append({"arxiv_id": k[0], "citation_key": k[1], "manual": gold, "auto": got, "why": x["note"]})
        if gold and got != gold:
            fn += 1
            if not got:
                errors.append({"arxiv_id": k[0], "citation_key": k[1], "manual": gold, "auto": "", "why": x["note"] or "no rule"})
    gold_pairs = sum(1 for g in truth.values() if g)
    print(f"{len(papers)} publications, {len(truth)} entries checked by hand, {gold_pairs} manual labels")
    print(f"automatic labels: {tp + fp}, correct {tp}, wrong {fp}, manual labels missed {fn}")
    print(f"precision {tp / max(1, tp + fp):.3f}, recall {tp / max(1, gold_pairs):.3f}")
    for rule in sorted(per_rule):
        c = per_rule[rule]
        print(f"  {rule}: {c['correct']} correct, {c['wrong']} wrong")
    for e in errors[:30]:
        print(f"  {e['arxiv_id']} {e['citation_key']}: manual={e['manual'] or '-'} auto={e['auto'] or '-'} ({e['why']})")
    if args.errors:
        with open(args.errors, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["arxiv_id", "citation_key", "manual", "auto", "why"])
            w.writeheader()
            w.writerows(errors)
        print(f"differences -> {args.errors}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", type=Path, required=True, help="M3 work folder (entries/references_clean, paper_stats...)")
    p.add_argument("--manual", type=Path, help="manual picks to leave out (default WORK/manual_pick.csv)")
    p.add_argument("--fraction", type=float, default=0.10, help="share of the publications outside the manual pool")
    p.add_argument("--min-n", type=int, default=5, help="least references with an arXiv id of a drawn publication")
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--fuzzy", type=float, default=0.85, help="R4: least title similarity")
    p.add_argument("--sim", choices=("chars", "words"), default="chars", help="R4: title similarity")
    p.add_argument("--margin", type=float, default=0.10, help="R4: least lead over the second best reference")
    p.add_argument("--out", type=Path, help="default WORK/labels_auto.csv")
    p.add_argument("--evaluate", type=Path, nargs="+", help="manual label files: measure the rules against them")
    p.add_argument("--errors", type=Path, help="with --evaluate: write the differences to this CSV")
    args = p.parse_args(argv)
    if args.evaluate:
        return cmd_evaluate(args)
    args.manual = args.manual or args.work / "manual_pick.csv"
    return cmd_label(args)


if __name__ == "__main__":
    sys.exit(main())
