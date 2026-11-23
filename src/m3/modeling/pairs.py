"""Milestone 3, modelling - shared helpers: loading the pairs, the feature sets, masking, MRR@5.

The data is WORK/features.parquet (features.py): one row per (entry, candidate
reference) pair, grouped by entry (`group`). Only the rows with is_sample = 1
(entries with a matched reference, the lecturer's definition of a label) are
used to train and to score; the other rows only get a ranking for the
predictions table. The part of each row (train / valid / test) comes from the
publication through WORK/splits.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features import FEATURE_GROUPS, FEATURES  # noqa: E402

# redundant or uninformative on train (feature_report.py): kept out by default
DROPPED = ["entry_id_in_paper", "entry_doi_in_paper", "entry_is_preprint", "year_gap_signed", "title_is_best"]
USED = [f for f in FEATURES if f not in DROPPED]
# compact set (ablation.py, cross-validation on train): one or two features per signal, the duplicates out.
# Title: 3-gram + TF-IDF (word Jaccard, exact, contain, length ratio add nothing on top); context: the gap to the
# best title and the author rank (the other ranks repeat the title features); venue: similarity only.
COMPACT = ["id_match", "doi_match", "entry_has_id", "entry_has_doi",
           "title_jacc3", "title_tfidf", "entry_title_missing",
           "first_author_match", "author_jacc", "author_overlap", "entry_authors_missing", "entry_authors_group",
           "year_gap", "year_missing", "venue_sim", "title_gap_best", "author_rank"]
FEATURE_SETS = {"full": USED, "compact": COMPACT}
LINK = FEATURE_GROUPS["link"]
# what a pair looks like when the entry carries no arXiv id and no DOI
NO_LINK = {"id_match": 0.0, "doi_match": 0.0, "entry_has_id": 0.0, "entry_has_doi": 0.0,
           "entry_id_in_paper": 0.0, "entry_doi_in_paper": 0.0, "entry_is_preprint": 0.0}
PARTS = ("train", "valid", "test")


class Pairs:
    """The pairs of features.parquet as numpy arrays, rows sorted by group (entry)."""

    def __init__(self, work: Path, columns: list[str] | None = None):
        self.columns = list(columns or FEATURES)
        t = pq.read_table(work / "features.parquet",
                          columns=["arxiv_id", "citation_key", "ref_arxiv_id", "group", "y", "is_sample", "source",
                                   "range"] + self.columns)
        group = np.asarray(t.column("group").to_pylist(), dtype=object)
        order = np.argsort(group, kind="stable")
        self.group = group[order]
        self.arxiv_id = np.asarray(t.column("arxiv_id").to_pylist(), dtype=object)[order]
        self.citation_key = np.asarray(t.column("citation_key").to_pylist(), dtype=object)[order]
        self.ref = np.asarray(t.column("ref_arxiv_id").to_pylist(), dtype=object)[order]
        self.y = t.column("y").to_numpy().astype(np.int8)[order]
        self.is_sample = t.column("is_sample").to_numpy().astype(bool)[order]
        self.source = np.asarray(t.column("source").to_pylist(), dtype=object)[order]
        self.range = np.asarray(t.column("range").to_pylist(), dtype=object)[order]
        self.X = np.column_stack([t.column(c).to_numpy(zero_copy_only=False).astype(np.float32)
                                  for c in self.columns])[order]
        splits = json.loads((work / "splits.json").read_text(encoding="utf-8"))
        part_of = {p: part for part in PARTS for p in splits[part]}
        self.part = np.asarray([part_of.get(p, "") for p in self.arxiv_id], dtype=object)
        # group index (0..G-1) and its boundaries
        _, self.gidx, self.gsize = np.unique(self.group, return_inverse=True, return_counts=True)

    def rows(self, part: str | None = None, source: str | None = None, samples: bool = True) -> np.ndarray:
        m = np.ones(len(self.y), dtype=bool)
        if part:
            m &= self.part == part
        if source:
            m &= self.source == source
        if samples:
            m &= self.is_sample
        return np.flatnonzero(m)

    def matrix(self, rows: np.ndarray, features: list[str], mask_link: bool | np.ndarray = False) -> np.ndarray:
        """X[rows] restricted to `features`; mask_link: True, or a boolean per row, sets the link features to
        their 'no id, no DOI' values (an entry that names no identifier)."""
        cols = [self.columns.index(f) for f in features]
        X = self.X[np.ix_(rows, cols)].copy()
        if mask_link is not False:
            which = np.ones(len(rows), bool) if mask_link is True else np.asarray(mask_link, bool)
            for f, v in NO_LINK.items():
                if f in features:
                    X[which, features.index(f)] = v
        return X

    def group_sizes(self, rows: np.ndarray) -> np.ndarray:
        """Sizes of the consecutive groups in rows (rows must be sorted, as rows() returns them)."""
        g = self.gidx[rows]
        change = np.flatnonzero(np.diff(g)) + 1
        return np.diff(np.concatenate([[0], change, [len(g)]]))


def ranks_of_gold(scores: np.ndarray, y: np.ndarray, gidx: np.ndarray) -> np.ndarray:
    """Per group with a gold pair: rank of the gold pair (1 = first). Ties count against the gold pair
    (a candidate scored like the gold one is placed before it), so a constant score never looks good."""
    gold = y == 1
    gold_score = np.full(gidx.max() + 1, np.nan)
    gold_score[gidx[gold]] = scores[gold]
    has_gold = ~np.isnan(gold_score)
    better = (~gold) & (scores >= gold_score[gidx])
    before = np.bincount(gidx[better], minlength=len(gold_score))
    return (before + 1)[has_gold]


def mrr_at_5(scores: np.ndarray, y: np.ndarray, gidx: np.ndarray) -> dict:
    r = ranks_of_gold(scores, y, gidx)
    if len(r) == 0:
        return {"entries": 0, "mrr5": float("nan"), "hit1": float("nan"), "hit5": float("nan")}
    rr = np.where(r <= 5, 1.0 / r, 0.0)
    return {"entries": int(len(r)), "mrr5": round(float(rr.mean()), 4), "hit1": round(float((r == 1).mean()), 4),
            "hit5": round(float((r <= 5).mean()), 4)}


def link_mask_for_groups(gidx: np.ndarray, share: float, seed: int) -> np.ndarray:
    """A boolean per row: the link features of a random `share` of the groups are masked (whole groups)."""
    groups = np.unique(gidx)
    rng = np.random.default_rng(seed)
    masked = rng.random(len(groups)) < share
    lookup = np.zeros(gidx.max() + 1, bool)
    lookup[groups[masked]] = True
    return lookup[gidx]
