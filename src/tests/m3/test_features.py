"""Tests of features.py; no network, a few seconds.

    python src/tests/m3/test_features.py
"""

from __future__ import annotations

import csv
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parents[2] / "m3"  # src/m3/
sys.path.insert(0, str(HERE))
import features as ft  # noqa: E402

PID = "2604.06000"


def tokens(title):  # same stop words on both sides, as text_clean.title_tokens does
    return [w for w in title.split() if w not in ("is", "all", "you", "of", "a")] if title else []


def entry(key, title, authors, year, doi=None, arxiv=None, journal=None, etype="article"):
    toks = tokens(title)
    return {"arxiv_id": PID, "citation_key": key, "entry_type": etype, "journal": journal, "title_clean": title,
            "title_tokens": toks, "authors_last": list(authors), "first_author_last": authors[0] if authors else None,
            "year": year, "doi": doi, "arxiv_id_in_entry": arxiv, "arxiv_id_from": "url" if arxiv else None}


def ref(rid, title, authors, year, doi=None, venue=None):
    return {"arxiv_id": PID, "ref_arxiv_id": rid, "title_clean": title, "title_tokens": tokens(title),
            "authors_last": list(authors), "year": year, "year_s2": year, "doi": doi, "venue": venue}


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        refs = [ref("1706.03762", "attention is all you need", ("vaswani", "shazeer"), 2017, venue="NeurIPS"),
                ref("2006.11239", "denoising diffusion probabilistic models", ("ho", "jain"), 2020),
                ref("2102.09672", "improved denoising diffusion probabilistic models", ("nichol", "dhariwal"), 2021)]
        entries = [entry("a", "attention is all you need", ("vaswani",), 2017, journal="NeurIPS"),
                   entry("b", "denoising diffusion probabilistic models", ("ho",), 2020, arxiv="2006.11239"),
                   entry("c", "a book about something", ("doe",), 1999, etype="book"),
                   entry("d", None, (), None)]
        pq.write_table(pa.Table.from_pylist(entries), self.work / "entries_clean.parquet")
        pq.write_table(pa.Table.from_pylist(refs), self.work / "references_clean.parquet")
        labels = [("a", "1706.03762"), ("b", "2006.11239"), ("c", ""), ("d", "")]
        with open(self.work / "labels.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["arxiv_id", "citation_key", "label_ref_arxiv_id", "is_match", "source", "labeler", "note", "range"])
            for k, l in labels:
                w.writerow([PID, k, l, "true" if l else "false", "manual", "T", "", "Bao"])
        ft.main(["--work", str(self.work)])
        self.t = {(r["citation_key"], r["ref_arxiv_id"]): r for r in
                  pq.read_table(self.work / "features.parquet").to_pylist()}

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_rows_and_labels(self):
        self.assertEqual(len(self.t), 4 * 3)  # every entry against every reference, false entries too
        self.assertEqual(sum(r["y"] for r in self.t.values()), 2)
        self.assertEqual({r["is_sample"] for k, r in self.t.items() if k[0] in "cd"}, {0})
        self.assertEqual(self.t[("a", "1706.03762")]["group"], f"{PID}|a")
        meta = json.loads((self.work / "features.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["samples"], 2)
        self.assertEqual(set(meta["groups"]), set(ft.FEATURE_GROUPS))

    def test_values(self):
        a = self.t[("a", "1706.03762")]
        self.assertEqual((a["title_exact"], a["title_jacc3"], a["first_author_match"], a["year_gap"]), (1, 1, 1, 0))
        self.assertAlmostEqual(a["title_tfidf"], 1.0, places=5)
        self.assertEqual((a["id_match"], a["entry_has_id"]), (0, 0))
        b = self.t[("b", "2006.11239")]
        self.assertEqual((b["id_match"], b["entry_id_in_paper"], b["entry_is_preprint"]), (1, 1, 1))
        # look-alike: high title similarity, other authors, other year
        b2 = self.t[("b", "2102.09672")]
        self.assertGreater(b2["title_jacc3"], 0.7)
        self.assertEqual(b2["first_author_match"], 0)
        self.assertEqual(b2["year_gap"], 1)
        d = self.t[("d", "1706.03762")]
        self.assertTrue(math.isnan(d["title_jacc3"]))
        self.assertEqual((d["entry_title_missing"], d["year_missing"], d["entry_authors_missing"]), (1, 1, 1))

    def test_context(self):
        a = [self.t[("a", rid)] for rid in ("1706.03762", "2006.11239", "2102.09672")]
        self.assertEqual([r["title_rank"] for r in a][0], 1)
        self.assertEqual(a[0]["title_is_best"], 1)
        self.assertEqual(a[0]["title_gap_best"], 0)
        self.assertGreater(a[0]["title_margin_second"], 0.5)
        self.assertEqual({r["n_candidates"] for r in a}, {3})
        b = [self.t[("b", rid)] for rid in ("2006.11239", "2102.09672")]
        self.assertEqual(b[0]["title_rank"], 1)
        self.assertEqual(b[1]["title_rank"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
