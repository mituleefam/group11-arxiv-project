"""Milestone 3, context experiment - step 1: the citation contexts of every BibTeX entry, from the M2 tree.

    python src/m3/context/contexts.py --tables M2_TABLES --out WORK/context [--papers N]

M2's `elements` and `hierarchy` tables (one tree per version of a paper) keep
the \\cite commands in the sentences, with the canonical citation keys of the
`bibtex` table (Member 3's citation_updater). So every sentence, list item or
figure caption that cites an entry is found by its key. One row per mention
(an element citing a key), in WORK/context/mentions.parquet:

  arxiv_id, citation_key  the entry (same key as entries_clean / labels.csv)
  element_id, kind        the citing element (sentence, figure, item, heading...)
  section                 type of the top-level heading above it: abstract,
                          intro, related, method, experiment, discussion,
                          appendix, front (before any heading), other
  headings                the heading path, cleaned ("related work llm bias")
  n_keys                  distinct keys cited in the element
  position                rank of the element in the reading order / number
                          of leaves of the version (0 = start, 1 = end)
  left, right             cleaned words just before / after the citation,
                          up to LEFT / RIGHT words, stopped at another citation
                          ("... standard deviation of recalls rstd [CITE]")
  sentence                the cleaned element around the citation (+-WINDOW
                          words); figures: their \\caption only
  neighbours              the previous and the next leaf under the same parent

Masking (no leak of the entry's metadata): a citation of the entry becomes
the word `zzcitezz`, any other citation `zzotherzz`; the keys themselves
(often "zheng2024large": author and year) never reach the text. Inline and
display math become `zzmathzz`; \\ref, \\label, \\eqref and URLs are dropped;
the rest goes through text_clean.normalize_text (lower case ASCII words).

Versions: elements are shared between versions (M2's dedup), so a mention is
the element, counted once; its place (section, headings, neighbours) is
taken in the latest version that holds it.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from text_clean import normalize_text  # noqa: E402

LEFT, RIGHT, WINDOW, NEIGHBOUR = 15, 8, 60, 40  # words
CITE, OTHER, MATH = "zzcitezz", "zzotherzz", "zzmathzz"
LEAVES = ("sentence", "formula", "figure")

_CITE_CMD = re.compile(r"\\[A-Za-z]*cite[A-Za-z]*\*?\s*(?:\[[^\[\]]*\]\s*){0,2}\{([^{}]*)\}")
_DROP_CMD = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref|pageref|label|url|footnotemark)\*?\s*\{[^{}]*\}")
_HREF = re.compile(r"\\href\s*\{[^{}]*\}")
_URL = re.compile(r"https?://\S+")
_MATH = re.compile(r"\$\$.*?\$\$|\$[^$]*\$|\\\(.*?\\\)|\\\[.*?\\\]"
                   r"|\\begin\{(equation|align|gather|multline|eqnarray|displaymath|math)\*?\}.*?\\end\{\1\*?\}", re.S)
_CAPTION = re.compile(r"\\caption\s*(?:\[[^\[\]]*\])?\s*\{")

SECTION_TYPES = [  # first match on the cleaned title of the top-level heading
    ("abstract", r"^abstract"),
    ("intro", r"introduction|^overview|^motivation"),
    ("related", r"related|background|literature|prior work|previous work|preliminar"),
    ("experiment", r"experiment|result|evaluation|ablation|benchmark|empirical|analysis|setup|implementation|"
                   r"performance|comparison|study|studies|simulation|numerical|dataset"),
    ("discussion", r"discussion|conclusion|limitation|future|summary|outlook|broader|impact"),
    ("method", r"method|approach|model|framework|proposed|architecture|algorithm|formulation|problem|theory|"
               r"design|system|setting|definition|notation"),
]
_SECTION_RX = [(name, re.compile(rx)) for name, rx in SECTION_TYPES]


def section_type(title: str) -> str:
    for name, rx in _SECTION_RX:
        if rx.search(title):
            return name
    return "other"


def caption_of(text: str) -> str:
    """The \\caption{...} of a float (balanced braces), else ''."""
    out = []
    for m in _CAPTION.finditer(text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            depth += c == "{"
            depth -= c == "}"
            i += 1
        out.append(text[m.end():i - 1])
    return " ".join(out)


def cited_keys(text: str) -> list[str]:
    return [k.strip() for m in _CITE_CMD.finditer(text) for k in m.group(1).split(",") if k.strip()]


def mask(text: str, key: str | None) -> str:
    """Cleaned words; the citations of `key` -> zzcitezz, other citations -> zzotherzz."""
    def cite(m):
        keys = {k.strip() for k in m.group(1).split(",")}
        return f" {CITE} " if key in keys else f" {OTHER} "
    s = _CITE_CMD.sub(cite, text)
    s = _HREF.sub(" ", _DROP_CMD.sub(" ", s))
    s = _URL.sub(" ", _MATH.sub(f" {MATH} ", s))
    return normalize_text(s)


def spans(words: list[str]) -> tuple[str, str, str]:
    """left, right, window around the first zzcitezz (merged with the next ones)."""
    at = [i for i, w in enumerate(words) if w == CITE]
    if not at:
        return "", "", " ".join(words[:2 * WINDOW])
    i = at[0]
    j = i - 1
    while j >= 0 and words[j] not in (OTHER, CITE) and i - j <= LEFT:
        j -= 1
    left = [w for w in words[j + 1:i] if w != MATH]
    k = at[-1] + 1
    while k < len(words) and words[k] not in (OTHER, CITE) and k - at[-1] <= RIGHT:
        k += 1
    right = [w for w in words[at[-1] + 1:k] if w != MATH]
    win = words[max(0, i - WINDOW):at[-1] + 1 + WINDOW]
    return " ".join(left), " ".join(right), " ".join(win)


def paper_mentions(arxiv_id: str, h: dict, kind: dict, content: dict) -> list[dict]:
    """h: version -> [(element_id, parent_id)] in reading order."""
    out, seen = [], set()
    for version in sorted(h, reverse=True):  # latest first
        edges = h[version]
        parent = dict(edges)
        children = defaultdict(list)
        for e, p in edges:
            children[p].append(e)
        leaves = [e for e, _ in edges if kind.get(e) in LEAVES]
        rank = {e: i for i, e in enumerate(leaves)}
        n_leaves = max(len(leaves), 1)
        for e, p in edges:
            text = content.get(e)
            if not text or "cite" not in text or e in seen:
                continue
            keys = list(dict.fromkeys(cited_keys(text)))
            if not keys:
                continue
            seen.add(e)
            k = kind.get(e)
            body = caption_of(text) if k == "figure" else text
            # heading path and section type
            path, a = [], p
            while a in parent:
                if kind.get(a) in ("heading", "paragraph"):
                    path.append(content.get(a) or "")
                a = parent[a]
            path.reverse()
            if not path:
                sec = "front"
            else:
                top = normalize_text(path[0])
                sec = "appendix" if top == "appendix" else section_type(top)
            heads = " ".join(normalize_text(x) for x in path)
            # neighbours: previous / next leaf under the same parent
            sib = children[p]
            i = sib.index(e)
            nb = []
            for j in (i - 1, i + 1):
                if 0 <= j < len(sib) and kind.get(sib[j]) == "sentence":
                    nb.append(" ".join(mask(content.get(sib[j]) or "", None).split()[:NEIGHBOUR]))
            for key in keys:
                words = mask(body, key).split()
                left, right, win = spans(words)
                out.append({"arxiv_id": arxiv_id, "citation_key": key, "element_id": e, "kind": k, "version": version,
                            "section": sec, "headings": heads, "n_keys": len(keys),
                            "position": round(rank.get(e, 0) / n_leaves, 4), "left": left, "right": right,
                            "sentence": win, "neighbours": " ".join(nb)})
    return out


SCHEMA = pa.schema([("arxiv_id", pa.string()), ("citation_key", pa.string()), ("element_id", pa.string()),
                    ("kind", pa.string()), ("version", pa.int32()), ("section", pa.string()),
                    ("headings", pa.string()), ("n_keys", pa.int32()), ("position", pa.float32()),
                    ("left", pa.string()), ("right", pa.string()), ("sentence", pa.string()),
                    ("neighbours", pa.string())])


def sorted_table(path: Path, columns: list[str]) -> tuple[pa.Table, np.ndarray, np.ndarray]:
    t = pq.read_table(path, columns=columns)
    t = t.take(pc.sort_indices(t, sort_keys=[("arxiv_id", "ascending")]))
    ids = t.column("arxiv_id").combine_chunks()
    uniq, starts = np.unique(np.asarray(ids.to_numpy(zero_copy_only=False)), return_index=True)
    return t, uniq, starts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tables", type=Path, required=True, help="M2 tables/ folder (elements/, hierarchy/)")
    p.add_argument("--out", type=Path, required=True, help="folder for mentions.parquet")
    p.add_argument("--papers", type=int, help="only the first N papers (a trial)")
    args = p.parse_args(argv)
    t0 = time.perf_counter()
    el, el_ids, el_start = sorted_table(next((args.tables / "elements").glob("*.parquet")),
                                        ["element_id", "arxiv_id", "kind", "content"])
    hi, hi_ids, hi_start = sorted_table(next((args.tables / "hierarchy").glob("*.parquet")),
                                        ["arxiv_id", "version", "element_id", "parent_id"])
    print(f"loaded {el.num_rows} elements, {hi.num_rows} edges, {len(hi_ids)} papers "
          f"({time.perf_counter() - t0:.0f} s)", flush=True)
    el_end = np.append(el_start[1:], el.num_rows)
    hi_end = np.append(hi_start[1:], hi.num_rows)
    el_at = {a: (s, e) for a, s, e in zip(el_ids, el_start, el_end)}
    args.out.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(args.out / "mentions.parquet", SCHEMA, compression="zstd")
    buf, n_rows, papers = [], 0, hi_ids[:args.papers] if args.papers else hi_ids
    for n, (a, hs, he) in enumerate(zip(papers, hi_start, hi_end)):
        part = hi.slice(hs, he - hs).to_pydict()
        h = defaultdict(list)
        for v, e, par in zip(part["version"], part["element_id"], part["parent_id"]):
            h[v].append((e, par))
        s, e = el_at.get(a, (0, 0))
        ep = el.slice(s, e - s).to_pydict()
        kind = dict(zip(ep["element_id"], ep["kind"]))
        content = dict(zip(ep["element_id"], ep["content"]))
        buf += paper_mentions(a, h, kind, content)
        if len(buf) > 200_000 or n == len(papers) - 1:
            writer.write_table(pa.Table.from_pylist(buf, schema=SCHEMA))
            n_rows += len(buf)
            buf = []
            print(f"  {n + 1}/{len(papers)} papers, {n_rows} mentions ({time.perf_counter() - t0:.0f} s)", flush=True)
    writer.close()
    print(f"-> {args.out / 'mentions.parquet'}: {n_rows} mentions ({time.perf_counter() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
