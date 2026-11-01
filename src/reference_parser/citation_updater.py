"""Member 3 - Milestone 2: rename citation keys in the text of a version.

    text = update(text, rename)        # rename: dedup_bibtex.dedup(...)[1]

Every \\cite-like command gets its keys renamed to the canonical key of
their reference: \\cite, \\citep, \\citet*, \\citealp, \\citeauthor,
\\citeyear, \\nocite, \\Cite, biblatex \\parencite, \\textcite, \\autocite,
\\footcite, multicites \\cites[..]{a}[..]{b}, with their optional notes
(\\citep[see][p.~3]{key}). Only the keys change: the command, its notes and
the order of the keys stay. A key cited twice in one command after the
renaming (\\cite{a,b} with a and b one work) is kept once.

A key is looked up as written, then case-insensitively (BibTeX keys are
case-insensitive) when that is unambiguous. \\nocite{*} is left alone.

This runs after dedup and before the sentences are compared (pipeline_m2
step 4), so two sentences that differ only by a citation key become equal
and get one element_id (Member 4).

LAST_STATS: commands and keys renamed by the last call.
"""

from __future__ import annotations

from reference_parser.bibitem_parser import iter_cite_groups

LAST_STATS: dict = {}


def _lookup(rename: dict[str, str]):
    lower: dict[str, str | None] = {}
    for old, new in rename.items():
        k = old.lower()
        lower[k] = new if k not in lower or lower[k] == new else None   # None: ambiguous
    def get(key: str) -> str | None:
        if key in rename:
            return rename[key]
        return lower.get(key.lower())
    return get


def rename_keys(keys: str, get) -> tuple[str, int]:
    """The key list of one command, renamed; and how many keys changed."""
    out, seen, changed = [], set(), 0
    parts = keys.split(",")
    for part in parts:
        key = part.strip()
        if not key:
            continue
        new = get(key) if key != "*" else None
        if new and new != key:
            changed += 1
            key = new
        if key in seen:
            changed += 1        # a duplicate left by the renaming
            continue
        seen.add(key)
        out.append(key)
    if not changed:
        return keys, 0
    return ",".join(out), changed


def update(text: str, rename: dict[str, str]) -> str:
    """The text with every \\cite-like key renamed (see the module doc)."""
    LAST_STATS.clear()
    LAST_STATS.update(commands=0, keys=0)
    if not rename or not text:
        return text
    get = _lookup(rename)
    out, pos = [], 0
    for start, end in iter_cite_groups(text):
        new, changed = rename_keys(text[start:end], get)
        if changed:
            out.append(text[pos:start])
            out.append(new)
            pos = end
            LAST_STATS["commands"] += 1
            LAST_STATS["keys"] += changed
    out.append(text[pos:])
    return "".join(out)
