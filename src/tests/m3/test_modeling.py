"""Tests of src/m3/modeling (MRR@5, masking, train / evaluate / predict end to end on a small fake set).

    python src/tests/m3/test_modeling.py
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parents[2] / "m3" / "modeling"  # src/m3/modeling/
sys.path.insert(0, str(HERE))
import pairs as common  # noqa: E402  (src/m3/modeling/pairs.py)
import evaluate  # noqa: E402
import predict  # noqa: E402
import train  # noqa: E402


class MetricTests(unittest.TestCase):
    def test_mrr(self):
        g = np.array([0, 0, 0, 1, 1, 1, 2, 2])
        y = np.array([1, 0, 0, 0, 1, 0, 0, 0])  # group 2 has no gold: not counted
        s = np.array([0.9, 0.1, 0.2, 0.5, 0.4, 0.3, 1.0, 0.0])
        m = common.mrr_at_5(s, y, g)
        self.assertEqual(m["entries"], 2)
        self.assertAlmostEqual(m["mrr5"], (1 + 1 / 2) / 2)
        self.assertEqual((m["hit1"], m["hit5"]), (0.5, 1.0))

    def test_ties_and_cutoff(self):
        g = np.zeros(7, int)
        y = np.array([1, 0, 0, 0, 0, 0, 0])
        self.assertEqual(common.mrr_at_5(np.zeros(7), y, g)["mrr5"], 0.0)  # a constant score never wins (rank 7)
        s = np.array([0.5, 0.9, 0.8, 0.7, 0.6, 0.1, 0.0])
        self.assertAlmostEqual(common.mrr_at_5(s, y, g)["mrr5"], 1 / 5)


def fake_work(root: Path, n_papers=40, n_entries=6, n_refs=8, seed=0) -> Path:
    """Pairs where the gold reference has the highest title similarity, plus noise; some entries have an id."""
    rng = np.random.default_rng(seed)
    rows, labels = [], []
    papers = [f"2604.{6000 + i:05d}" for i in range(n_papers)]
    for pi, pid in enumerate(papers):
        source = "manual" if pi < 6 else "auto"
        for e in range(n_entries):
            key = f"k{e}"
            matched = e < n_entries - 1  # the last entry of every paper has no reference
            gold = e % n_refs
            has_id = rng.random() < 0.4
            for r in range(n_refs):
                is_gold = matched and r == gold
                f = {name: 0.0 for name in common.FEATURES}
                f["title_jacc3"] = (0.6 + 0.4 * rng.random()) if is_gold else 0.5 * rng.random()
                f["title_jacc_word"] = f["title_tfidf"] = f["title_jacc3"]
                f["author_jacc"] = (0.5 + 0.5 * rng.random()) if is_gold else 0.3 * rng.random()
                f["year_gap"] = 0.0 if is_gold else float(rng.integers(0, 6))
                f["entry_has_id"] = float(has_id)
                f["id_match"] = float(has_id and is_gold)
                f["n_candidates"] = float(n_refs)
                rows.append({"arxiv_id": pid, "citation_key": key, "ref_arxiv_id": f"2001.{r:05d}",
                             "group": f"{pid}|{key}", "y": int(is_gold), "is_sample": int(matched),
                             "source": source, "range": "Bao", **f})
            labels.append({"arxiv_id": pid, "citation_key": key, "label_ref_arxiv_id": f"2001.{gold:05d}" if matched else "",
                           "is_match": "true" if matched else "false", "source": source, "labeler": "x", "note": "",
                           "range": "Bao"})
    schema = pa.schema([(c, pa.string()) for c in ("arxiv_id", "citation_key", "ref_arxiv_id", "group")] +
                       [("y", pa.int8()), ("is_sample", pa.int8()), ("source", pa.string()), ("range", pa.string())] +
                       [(f, pa.float32()) for f in common.FEATURES])
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), root / "features.parquet")
    with open(root / "labels.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(labels[0]))
        w.writeheader()
        w.writerows(labels)
    (root / "splits.json").write_text(json.dumps({"train": papers[2:36] + papers[:0], "valid": [papers[0], papers[36], papers[37]],
                                                  "test": [papers[1], papers[38], papers[39]]}), encoding="utf-8")
    return root


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = fake_work(Path(tempfile.mkdtemp()))
        assert train.main(["--work", str(cls.work), "--folds", "2"]) == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_train_outputs(self):
        report = json.loads((self.work / "train_report.json").read_text(encoding="utf-8"))
        chosen = json.loads((self.work / "models" / "chosen.json").read_text(encoding="utf-8"))["model"]
        self.assertEqual(report["chosen"], chosen)
        self.assertTrue((self.work / "models" / f"{chosen}.pkl").exists())
        self.assertGreater(report["cv"][chosen]["all"]["mrr5"], 0.9)  # the fake signal is easy
        self.assertNotIn("test", json.dumps(report).replace('"test"', ""))  # nothing of test in the report

    def test_masking(self):
        pairs = common.Pairs(self.work)
        rows = pairs.rows("train")
        X = pairs.matrix(rows, common.USED, True)
        for f in ("id_match", "entry_has_id"):
            self.assertEqual(float(np.abs(X[:, common.USED.index(f)]).sum()), 0.0)
        self.assertGreater(float(pairs.matrix(rows, common.USED)[:, common.USED.index("id_match")].sum()), 0)

    def test_evaluate_and_predict(self):
        self.assertEqual(evaluate.main(["--work", str(self.work)]), 0)
        ev = json.loads((self.work / "evaluation_valid.json").read_text(encoding="utf-8"))
        self.assertEqual(ev["part"], "valid")
        self.assertFalse((self.work / "evaluation_test.json").exists())  # test only on request
        self.assertEqual(predict.main(["--work", str(self.work)]), 0)
        t = pq.read_table(self.work / "predictions.parquet").to_pylist()
        self.assertEqual(len(t), 40 * 6)  # every entry, labelled or not
        self.assertEqual(list(t[0])[:9], ["arxiv_id", "partition", "citation_key", "groundtruth_arxiv_id",
                                          "cand_1", "cand_2", "cand_3", "cand_4", "cand_5"])
        no_ref = [r for r in t if r["citation_key"] == "k5"]
        self.assertTrue(all(r["groundtruth_arxiv_id"] is None for r in no_ref))
        self.assertTrue(all(r["cand_5"] is not None for r in t))  # 8 candidates each
        self.assertEqual({r["partition"] for r in t}, {"train", "valid", "test"})
        hits = [r["cand_1"] == r["groundtruth_arxiv_id"] for r in t if r["groundtruth_arxiv_id"]]
        self.assertGreater(sum(hits) / len(hits), 0.9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
