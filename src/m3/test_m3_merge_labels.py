"""Tests of merge_labels.py; no network, a few seconds.

    python src/m3/test_m3_merge_labels.py
"""

from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import merge_labels as ml  # noqa: E402
import split  # noqa: E402

MANUAL = [f"2604.{6000 + i:05d}" for i in range(5)]  # Bao range
AUTO = [f"2604.{7000 + i:05d}" for i in range(3)]


def write_csv(path, rows, cols=None):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols or list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def label(pid, key, ref, source):
    return {"arxiv_id": pid, "citation_key": key, "label_ref_arxiv_id": ref, "is_match": "true" if ref else "false",
            "source": source, "labeler": "T" if source == "manual" else "auto", "note": ""}


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        entries, refs, stats, status = [], [], [], []
        for pid in MANUAL + AUTO + ["2604.08000"]:
            for j in range(25):
                entries.append({"arxiv_id": pid, "citation_key": f"k{j}"})
                refs.append({"arxiv_id": pid, "ref_arxiv_id": f"2001.{j:05d}"})
            stats.append({"arxiv_id": pid, "m": 25, "n": 25})
            status.append({"arxiv_id": pid, "status": "success"})
        pq.write_table(pa.Table.from_pylist(entries), self.work / "entries_clean.parquet")
        pq.write_table(pa.Table.from_pylist(refs), self.work / "references_clean.parquet")
        write_csv(self.work / "paper_stats.csv", stats)
        write_csv(self.work / "papers_refs.csv", status)
        write_csv(self.work / "manual_pick.csv", [{"range": "Bao", "arxiv_id": p} for p in MANUAL])
        manual = [label(p, f"k{j}", f"2001.{j:05d}" if j < 21 else "", "manual") for p in MANUAL for j in range(25)]
        auto = [label(p, f"k{j}", f"2001.{j:05d}" if (j < 10 and p != AUTO[2]) else "", "auto")
                for p in AUTO for j in range(25)]
        write_csv(self.work / "labels_manual_T.csv", manual)
        write_csv(self.work / "labels_auto.csv", auto)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def run_merge(self, *extra):
        return ml.main(["--work", str(self.work), *extra])

    def test_incomplete_and_outputs(self):
        self.assertEqual(self.run_merge(), 0)  # 5 manual publications of >= 20 labels, >= 10% automatic
        rows = list(csv.DictReader(open(self.work / "labels_manual_T.csv", encoding="utf-8")))
        write_csv(self.work / "labels_manual_T.csv", [r for r in rows if r["arxiv_id"] != MANUAL[4]])
        (self.work / "labels.csv").unlink()
        self.assertEqual(self.run_merge(), 1)  # 4 manual publications: an error ...
        self.assertFalse((self.work / "labels.csv").exists())
        self.assertEqual(self.run_merge("--allow-incomplete"), 0)  # ... or a warning while labelling goes on
        rows = list(csv.DictReader(open(self.work / "labels.csv", encoding="utf-8")))
        self.assertEqual(list(rows[0]), ml.OUT_COLS)
        self.assertEqual(len(rows), 7 * 25)  # false rows are kept too
        self.assertEqual({r["range"] for r in rows}, {"Bao"})
        rank = {r["arxiv_id"]: int(r["n"]) for r in csv.DictReader(open(self.work / "split_rankable.csv", encoding="utf-8"))}
        self.assertEqual(rank[AUTO[2]], 0)  # no label at all: not for test / valid
        self.assertEqual(rank[AUTO[0]], 25)

    def test_suspects(self):
        (self.work / "s.txt").write_text(AUTO[0], encoding="utf-8")
        self.assertEqual(self.run_merge("--allow-incomplete", "--suspects", str(self.work / "s.txt")), 0)
        rank = {r["arxiv_id"]: int(r["n"]) for r in csv.DictReader(open(self.work / "split_rankable.csv", encoding="utf-8"))}
        self.assertEqual(rank[AUTO[0]], 0)

    def test_errors(self):
        rows = list(csv.DictReader(open(self.work / "labels_auto.csv", encoding="utf-8")))
        bad = rows + [label(MANUAL[0], "k1", "2001.00001", "auto"),  # manual pick in the auto labels + duplicate
                      label(AUTO[0], "zz", "", "auto"),  # not an entry
                      label(AUTO[1], "k20", "1999.99999", "auto")]  # not a reference of the paper
        bad = [b for b in bad if not (b["arxiv_id"] == AUTO[1] and b["citation_key"] == "k20" and b["note"] == "")] + [bad[-1]]
        write_csv(self.work / "labels_auto.csv", bad)
        _, errors, _, _, _ = ml.merge(self.work, [self.work / "labels_manual_T.csv"], self.work / "labels_auto.csv",
                                      set(), True)
        text = "\n".join(errors)
        self.assertIn("both manual and automatic", text)
        self.assertIn("manual picks with automatic labels", text)
        self.assertIn("2 rows", text)
        self.assertIn("not an entry", text)
        self.assertIn("is not a reference", text)
        self.assertEqual(self.run_merge("--allow-incomplete"), 1)
        self.assertFalse((self.work / "labels.csv").exists())

    def test_manual_minimum(self):
        rows = list(csv.DictReader(open(self.work / "labels_manual_T.csv", encoding="utf-8")))
        for r in rows:
            if r["arxiv_id"] == MANUAL[0] and int(r["citation_key"][1:]) >= 15:
                r["label_ref_arxiv_id"], r["is_match"] = "", "false"
        write_csv(self.work / "labels_manual_T.csv", rows)
        _, errors, _, _, _ = ml.merge(self.work, [self.work / "labels_manual_T.csv"], self.work / "labels_auto.csv",
                                      set(), False)
        self.assertTrue(any(f"{MANUAL[0]}: 15 labels" in e for e in errors))


class SplitPerRangeTests(unittest.TestCase):
    def test_one_of_each_kind_per_range(self):
        sources = {}
        for first in ("2511.21709", "2512.13231", "2601.13156", "2604.05535", "2605.12338"):  # one paper id per range
            yymm, num = first.split(".")
            for i in range(4):
                sources[f"{yymm}.{int(num) + i:05d}"] = "manual"
            for i in range(10, 30):
                sources[f"{yymm}.{int(num) + i:05d}"] = "auto"
        res = split.make_split_per_range(sources, 11)
        sys.path.insert(0, str(HERE))
        from select_manual import range_of
        for part in ("test", "valid"):
            self.assertEqual(sorted((range_of(p), sources[p]) for p in res[part]),
                             sorted((r, k) for r in ("Tram", "Thu", "Phat", "Bao", "Quan") for k in ("manual", "auto")))
        self.assertFalse(set(res["test"]) & set(res["valid"]) or set(res["test"]) & set(res["train"]))
        self.assertEqual(len(res["train"]) + 20, len(sources))
        self.assertEqual(res, split.make_split_per_range(sources, 11))  # same seed, same split
        not_rankable = {p for p in sources if range_of(p) == "Bao" and sources[p] == "manual"}
        with self.assertRaises(ValueError):
            split.make_split_per_range(sources, 11, set(sources) - not_rankable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
