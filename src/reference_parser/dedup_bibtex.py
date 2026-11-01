"""Member 3 - Milestone 2: one row per reference of a paper, across its versions.

    rows, rename = dedup(by_version)
        by_version  {version number: bibtex_converter.convert(...) rows}
        rows        one row per distinct reference (fields unionized)
        rename      {old citation key: canonical key}, for citation_updater

What counts as the same reference (two rows are joined when one rule holds,
and joins are transitive, with a union-find):

  1. the same citation key in two versions, unless the two rows clearly
     disagree (different DOIs, different arXiv ids, or titles that are not
     alike): a key reused for another work, as \\bibitem{1} often is;
  2. the same DOI, or the same arXiv id, or the same URL naming a page,
     unless the titles clearly disagree (a wrong DOI picked from free text);
  3. the same title (lower case, no accents or punctuation; at least 3 words
     or 15 characters) with compatible years (equal, or one missing) and
     compatible first authors (same family name, or one missing);
  4. nearly the same title (difflib ratio >= 0.92, compared only between rows
     sharing a rare title word) with the same year or the same first author.

Within one version two keys can be joined too (the task: "duplicated entries
inside each version or across different versions").

The canonical key of a group is the key used by the most versions, then the
one of the latest version (the text readers see last): in the example of the
task, vaswani2017 (v1) and vit_transformer (v2, v3) give vit_transformer.
Canonical keys are unique in a paper (case-insensitive, as in BibTeX); a key
already taken by another group gets a suffix (_2, _3, ...).

Field union: each field takes the best value of the group, never a merge of
two different values: a structured source (.bib, amsrefs, \\bibinfo) before a
\\bibitem guess, then the value most rows agree on, then the latest version,
then the longest. entry_type "misc" only when no row says more. So
v1 {title, author, year} + v2 {doi} + v3 {no year} -> {title, author, year, doi}.

LAST_STATS: counts of the last call (rows in, rows out, renames, conflicts).
"""

from __future__ import annotations

import difflib
import logging
from collections import Counter, defaultdict

from reference_parser.ref_text import find_arxiv_id, normalize_doi, normalize_url, surname_key, title_key

log = logging.getLogger("m2.references")

FIELDS = ["citation_key", "entry_type", "title", "authors", "year", "doi", "journal", "url"]
VALUE_FIELDS = ["entry_type", "title", "authors", "year", "doi", "journal", "url"]
TITLE_FUZZY = 0.92      # difflib ratio for nearly equal titles
TITLE_UNLIKE = 0.6      # below this, two titles name different works
STOP = {"a", "an", "the", "of", "on", "in", "for", "and", "to", "with", "by", "from", "at", "via",
        "is", "are", "its", "using", "towards", "toward", "into", "as", "we", "our", "new"}
LAST_STATS: dict = {}


class _Ref:
    """One input row with what is compared."""
    __slots__ = ("version", "index", "row", "key", "doi", "arxiv", "url", "title", "year", "surname")

    def __init__(self, version: int, index: int, row: dict):
        self.version, self.index, self.row = version, index, row
        self.key = (row.get("citation_key") or "").strip()
        self.doi = normalize_doi(row.get("doi"))
        self.arxiv = (find_arxiv_id(row.get("url")) or find_arxiv_id(row.get("doi"))
                      or find_arxiv_id(row.get("journal")))
        self.url = normalize_url(row.get("url"))
        if self.url and ("doi.org/" in self.url or "arxiv.org/" in self.url):
            self.url = None            # compared through doi / arxiv already
        t = title_key(row.get("title"))
        self.title = t if (len(t.split()) >= 3 or len(t) >= 15) else ""
        self.year = row.get("year")
        authors = row.get("authors") or []
        self.surname = surname_key(authors[0]) if authors else ""


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[max(ra, rb)] = min(ra, rb)   # the earliest row stays the root
        return True


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 1.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _disagree(a: _Ref, b: _Ref) -> bool:
    """Two rows that cannot be the same work."""
    if a.doi and b.doi and a.doi != b.doi:
        return True
    if a.arxiv and b.arxiv and a.arxiv != b.arxiv:
        return True
    if a.title and b.title and _similar(a.title, b.title) < TITLE_UNLIKE:
        return True
    return False


def _compatible(a: _Ref, b: _Ref) -> bool:
    if a.year and b.year and abs(a.year - b.year) > 1:   # a preprint year and the published one
        return False
    if a.surname and b.surname and a.surname != b.surname:
        return False
    return True


def _groups(refs: list[_Ref]) -> tuple[_UnionFind, Counter]:
    uf = _UnionFind(len(refs))
    why: Counter = Counter()

    def join(indexes: list[int], rule: str, check) -> None:
        first = indexes[0]
        for other in indexes[1:]:
            if check(refs[first], refs[other]) and uf.union(first, other):
                why[rule] += 1

    by: dict[str, dict] = {name: defaultdict(list) for name in ("key", "doi", "arxiv", "url", "title")}
    for i, r in enumerate(refs):
        if r.key:
            by["key"][r.key.lower()].append(i)
        if r.doi:
            by["doi"][r.doi].append(i)
        if r.arxiv:
            by["arxiv"][r.arxiv].append(i)
        if r.url:
            by["url"][r.url].append(i)
        if r.title:
            by["title"][r.title].append(i)

    for name in ("doi", "arxiv", "url"):
        for indexes in by[name].values():
            if len(indexes) > 1:
                join(indexes, name, lambda a, b: not (a.title and b.title and _similar(a.title, b.title) < TITLE_UNLIKE))
    for indexes in by["title"].values():
        if len(indexes) > 1:
            join(indexes, "title", lambda a, b: _compatible(a, b) and not _disagree(a, b))
    for indexes in by["key"].values():
        if len(indexes) > 1:
            # each row with the same key joins the first one it agrees with
            for k, i in enumerate(indexes[1:], 1):
                for j in indexes[:k]:
                    if not _disagree(refs[i], refs[j]):
                        if uf.union(i, j):
                            why["key"] += 1
                        break

    # nearly equal titles: compared only between rows that share a rare word
    words: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(refs):
        if r.title:
            for w in sorted((w for w in set(r.title.split()) if w not in STOP and len(w) > 3),
                            key=len, reverse=True)[:3]:
                words[w].append(i)
    done = set()
    for indexes in words.values():
        if len(indexes) > 60:          # a word in very many titles tells little; skip the block
            continue
        for k, i in enumerate(indexes):
            for j in indexes[k + 1:]:
                pair = (i, j) if i < j else (j, i)
                if pair in done or uf.find(i) == uf.find(j):
                    continue
                done.add(pair)
                a, b = refs[i], refs[j]
                same_year = bool(a.year and a.year == b.year)
                same_author = bool(a.surname and a.surname == b.surname)
                if (same_year or same_author) and _compatible(a, b) and not _disagree(a, b) \
                        and _similar(a.title, b.title) >= TITLE_FUZZY:
                    if uf.union(i, j):
                        why["fuzzy_title"] += 1
    return uf, why


def _norm(value):
    if isinstance(value, list):
        return tuple(surname_key(a) for a in value)
    if isinstance(value, str):
        return title_key(value) or value.strip().lower()
    return value


def _best(members: list[_Ref], field: str):
    """The value of `field` for a group (see the module doc)."""
    candidates = []
    for r in members:
        value = r.row.get(field)
        if value in (None, "", []):
            continue
        if field == "entry_type" and value == "misc":
            continue
        candidates.append((r, value))
    if not candidates:
        return "misc" if field == "entry_type" else ([] if field == "authors" else None)
    agree = Counter(_norm(v) for _, v in candidates)

    def rank(item):
        r, value = item
        structured = r.row.get("_source") == "structured"
        size = len(value) if isinstance(value, (list, str)) else 0
        return (structured, agree[_norm(value)], r.version, size)

    return max(candidates, key=rank)[1]


def _canonical(members: list[_Ref]) -> list[str]:
    """The keys of a group, best first."""
    versions: dict[str, set] = defaultdict(set)
    latest: dict[str, tuple] = {}
    spelled: dict[str, str] = {}
    for r in members:
        if not r.key:
            continue
        low = r.key.lower()
        versions[low].add(r.version)
        latest[low] = max(latest.get(low, (-1, 0)), (r.version, -r.index))
        spelled[low] = r.key                       # rows come in version order: the latest spelling stays
    order = sorted(versions, key=lambda k: (len(versions[k]), latest[k]), reverse=True)
    return [spelled[k] for k in order]


def dedup(by_version: dict[int, list[dict]]) -> tuple[list[dict], dict[str, str]]:
    """(rows, rename): see the module doc."""
    refs = [_Ref(int(v), i, row) for v in sorted(by_version, key=int)
            for i, row in enumerate(by_version[v] or [])]
    if not refs:
        LAST_STATS.clear()
        LAST_STATS.update(rows_in=0, rows_out=0, renamed_keys=0, key_conflicts=0)
        return [], {}
    uf, why = _groups(refs)
    groups: dict[int, list[_Ref]] = defaultdict(list)
    for i, r in enumerate(refs):
        groups[uf.find(i)].append(r)

    used: set[str] = set()
    owners: dict[str, set] = defaultdict(set)      # old key (lower) -> groups that use it
    chosen: dict[int, str] = {}
    for root in sorted(groups):                    # first seen first
        members = groups[root]
        for r in members:
            if r.key:
                owners[r.key.lower()].add(root)
        keys = _canonical(members)
        key = next((k for k in keys if k.lower() not in used), None)
        if key is None:
            base = keys[0] if keys else "ref"
            n = 2
            while f"{base}_{n}".lower() in used:
                n += 1
            key = f"{base}_{n}"
            log.info("citation key %r names two different works: the later one becomes %r", base, key)
        used.add(key.lower())
        chosen[root] = key

    rows, rename, conflicts = [], {}, 0
    for root in sorted(groups):
        members = groups[root]
        row = {"citation_key": chosen[root]}
        for name in VALUE_FIELDS:
            row[name] = _best(members, name)
        rows.append(row)
        for r in members:
            if not r.key or r.key == chosen[root]:
                continue
            if len(owners[r.key.lower()]) > 1:     # the same key names different works: no rename
                conflicts += 1
                continue
            rename[r.key] = chosen[root]
    conflict_keys = sum(len(g) > 1 for g in owners.values())
    if conflict_keys:
        log.info("%d citation key(s) name different works in different versions: their \\cite are left as they are",
                 conflict_keys)
    LAST_STATS.clear()
    LAST_STATS.update(rows_in=len(refs), rows_out=len(rows), renamed_keys=len(rename),
                      key_conflicts=conflict_keys, versions=len(by_version),
                      joined_by_key=why["key"], joined_by_doi=why["doi"], joined_by_arxiv=why["arxiv"],
                      joined_by_url=why["url"], joined_by_title=why["title"],
                      joined_by_fuzzy_title=why["fuzzy_title"])
    return rows, rename
