"""Tests of the automatic labelling (auto_label.py); no network, a few seconds.

    python src/tests/m3/test_auto_label.py
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

HERE = Path(__file__).resolve().parents[2] / "m3"  # src/m3/
sys.path.insert(0, str(HERE))
import auto_label as al  # noqa: E402


def e(key, title, authors=("smith",), year=2020, doi=None, arxiv=None, pid="2604.06000"):
    return {"arxiv_id": pid, "citation_key": key, "title_clean": title, "authors_last": list(authors),
            "first_author_last": authors[0] if authors else None, "year": year, "doi": doi, "arxiv_id_in_entry": arxiv}


def r(rid, title, authors=("smith",), year=2020, doi=None, pid="2604.06000"):
    return {"arxiv_id": pid, "ref_arxiv_id": rid, "title_clean": title, "authors_last": list(authors),
            "year": year, "year_s2": year, "doi": doi}


REFS = [r("2001.00001", "attention is all you need", ("vaswani",), 2017),
        r("2001.00002", "denoising diffusion probabilistic models", ("ho",), 2020),
        r("2001.00003", "improved denoising diffusion probabilistic models", ("nichol",), 2021),
        r("2001.00004", "toward generalist neural motion planners for robotic manipulators", ("soleymanzadeh",), 2026),
        r("2001.00005", "a dataset for driving", ("li",), 2019, doi="10.1109/x.1"),
        r("2001.00006", "batch informed trees", ("gammell",), 2017)]


class RuleTests(unittest.TestCase):
    def run_rules(self, entries):
        return {x["citation_key"]: x for x in al.label_paper(entries, al.Paper(REFS), 0.85, 0.10)}

    def test_rules(self):
        out = self.run_rules([
            e("id", "whatever", arxiv="2001.00001"),
            e("doi", "a different title here", ("li",), 2019, doi="10.1109/x.1"),
            e("arxivdoi", "something", doi="10.48550/arxiv.2001.00003"),
            e("title", "denoising diffusion probabilistic models", ("ho",), 2020),
            e("journal", "batch informed trees", ("gammell",), 2020),  # journal version 3 years later, same authors
            e("fuzzy", "towards generalist neural motion planners for robotic manipulators", ("soleymanzadeh",), 2026),
            e("none", "a book about something else", ("doe",), 1999),
        ])
        self.assertEqual({k: (x["label_ref_arxiv_id"], x["note"]) for k, x in out.items()}, {
            "id": ("2001.00001", "R1"), "doi": ("2001.00005", "R2"), "arxivdoi": ("2001.00003", "R2"),
            "title": ("2001.00002", "R3"), "journal": ("2001.00006", "R3"),
            "fuzzy": ("2001.00004", "R4"), "none": ("", "")})
        self.assertEqual(out["none"]["is_match"], "false")
        self.assertEqual({x["source"] for x in out.values()}, {"auto"})

    def test_look_alike_is_not_matched(self):
        # "Improved DDPM" cited, by another first author: R4 must not take the DDPM reference
        out = self.run_rules([e("x", "improved denoising diffusion probabilistic model", ("ho",), 2020)])
        self.assertNotEqual(out["x"]["label_ref_arxiv_id"], "2001.00002")
        # same title but other authors and years far apart: no R3
        out = self.run_rules([e("y", "batch informed trees", ("doe",), 2010)])
        self.assertEqual(out["y"]["label_ref_arxiv_id"], "")
        # split name, as M2 writes it: "M 2 diffuser" is "M2Diffuser" for R4
        refs = REFS + [r("2001.00007", "m2diffuser diffusion based trajectory optimization", ("yan",), 2024)]
        out = {x["citation_key"]: x for x in al.label_paper(
            [e("y", "m 2 diffuser diffusion based trajectory optimization", ("yan",), 2025)], al.Paper(refs), 0.85, 0.10)}
        self.assertEqual((out["y"]["label_ref_arxiv_id"], out["y"]["note"]), ("2001.00007", "R4"))
        out = self.run_rules([e("y", "batch informed trees", ("doe",), 2010)])
        self.assertEqual(out["y"]["label_ref_arxiv_id"], "")

    def test_conflicts(self):
        out = self.run_rules([e("c", "attention is all you need", ("vaswani",), 2017, arxiv="2001.00002")])
        self.assertEqual(out["c"]["label_ref_arxiv_id"], "")
        self.assertTrue(out["c"]["note"].startswith("conflict"))
        # id entry with a garbled title + title entry, same reference: the same work cited twice, both labels
        out = self.run_rules([e("a", "x", arxiv="2001.00001"),
                              e("b", "attention is all you need", ("vaswani",), 2017)])
        self.assertEqual([out[k]["label_ref_arxiv_id"] for k in "ab"], ["2001.00001", "2001.00001"])
        self.assertTrue(all("shared" in out[k]["note"] for k in "ab"))
        out = self.run_rules([e("a", "x", arxiv="2001.00001"), e("b", "y", arxiv="2001.00001")])  # cited twice
        self.assertTrue(all(x["is_match"] == "true" and "shared" in x["note"] for x in out.values()))
        # renamed paper: the id entry keeps an older title of the same work (same authors): kept
        out = self.run_rules([e("a", "denoising diffusion models are great", ("ho",), 2020, arxiv="2001.00002"),
                              e("b", "denoising diffusion probabilistic models", ("ho",), 2020)])
        self.assertEqual([out[k]["label_ref_arxiv_id"] for k in "ab"], ["2001.00002", "2001.00002"])

    def test_short_title_other_authors(self):
        refs = REFS + [r("2001.00008", "generative adversarial networks", ("cohen", "giryes"), 2021),
                       r("2001.00009", "qwen3 technical report", ("yang", "li"), 2025),
                       r("2001.00010", "integrable systems and factorization problems", ("shansky",), 2002)]
        out = {x["citation_key"]: x for x in al.label_paper(
            [e("gan", "generative adversarial networks", ("goodfellow", "abadie"), 2020),  # same title, other work
             e("qwen", "qwen3 technical report", ("team qwen",), 2025),  # organisation as author
             e("tr", "integrable systems and factorization problems", ("shanskii",), 2003)],  # transliteration
            al.Paper(refs), 0.85, 0.10)}
        self.assertEqual(out["gan"]["label_ref_arxiv_id"], "")
        self.assertEqual(out["qwen"]["label_ref_arxiv_id"], "2001.00009")
        self.assertEqual(out["tr"]["label_ref_arxiv_id"], "2001.00010")

    def test_full_title(self):
        for t in ("phys a 20 3765 hep ex 0409052", "chaos solitons fractals 202", "m e j newman",
                  "he and x d ji", "arxiv 2310 10746", "and d"):
            self.assertFalse(al.full_title(t), t)
        for t in ("a benchmark for robotic biology laboratories", "stellar and black hole assembly in z 0 3 infrared luminous galaxies"):
            self.assertTrue(al.full_title(t), t)

    def test_id_mismatch(self):
        # an id pasted from another paper of the same group, while another entry is that paper: the id loses
        out = self.run_rules([e("a", "a benchmark for robotic biology laboratories", ("vaswani",), 2017,
                                arxiv="2001.00001"),
                              e("b", "attention is all you need", ("vaswani",), 2017)])
        self.assertEqual(out["a"]["label_ref_arxiv_id"], "")
        self.assertTrue(out["a"]["note"].startswith("id_mismatch R1->2001.00001"))
        self.assertEqual((out["b"]["label_ref_arxiv_id"], out["b"]["note"]), ("2001.00001", "R3"))
        # alone: other title and other authors -> no label; other title but same authors -> kept (renamed)
        out = self.run_rules([e("a", "a benchmark for robotic biology laboratories", ("doe",), 2017, arxiv="2001.00001")])
        self.assertTrue(out["a"]["note"].startswith("id_mismatch"))
        out = self.run_rules([e("a", "a benchmark for robotic biology laboratories", ("vaswani",), 2017,
                                arxiv="2001.00001")])
        self.assertEqual((out["a"]["label_ref_arxiv_id"], out["a"]["note"]), ("2001.00001", "R1"))


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        entries, refs, status, stats = [], [], [], []
        for i in range(40):
            pid = f"2604.{6000 + i:05d}"
            n = 0 if i % 10 == 0 else 6
            for j in range(n):
                refs.append(r(f"2001.{j:05d}", f"reference title {j}", pid=pid))
            for j in range(8):
                entries.append(e(f"k{j}", f"reference title {j}", pid=pid))
            status.append({"arxiv_id": pid, "status": "success"})
            stats.append({"arxiv_id": pid, "m": 8, "n": n})
        full = lambda rows, extra: [{**x, **extra} for x in rows]
        pq.write_table(pa.Table.from_pylist(full(entries, {"title": None})), self.work / "entries_clean.parquet")
        pq.write_table(pa.Table.from_pylist(refs), self.work / "references_clean.parquet")
        for name, rows in (("papers_refs.csv", status), ("paper_stats.csv", stats)):
            with open(self.work / name, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        with open(self.work / "manual_pick.csv", "w", encoding="utf-8", newline="") as f:
            f.write("range,arxiv_id\nBao,2604.06001\nBao,2604.06002\n")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_pool_and_output(self):
        self.assertEqual(al.main(["--work", str(self.work), "--fraction", "0.25"]), 0)
        rows = list(csv.DictReader(open(self.work / "labels_auto.csv", encoding="utf-8")))
        papers = {x["arxiv_id"] for x in rows}
        self.assertEqual(len(papers), 10)  # ceil(0.25 * (40 - 2))
        self.assertFalse(papers & {"2604.06001", "2604.06002"})  # manual picks never go to the automatic pool
        self.assertFalse(papers & {f"2604.{6000 + i:05d}" for i in range(0, 40, 10)})  # n = 0 is not drawn
        self.assertEqual(list(rows[0]), al.LABEL_COLS)
        again = list(csv.DictReader(open(self.work / "labels_auto.csv", encoding="utf-8")))
        al.main(["--work", str(self.work), "--fraction", "0.25"])
        self.assertEqual(rows, list(csv.DictReader(open(self.work / "labels_auto.csv", encoding="utf-8"))))
        self.assertEqual(rows, again)

    def test_evaluate(self):
        manual = self.work / "manual.csv"
        with open(manual, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=al.LABEL_COLS)
            w.writeheader()
            for j in range(8):
                w.writerow({"arxiv_id": "2604.06001", "citation_key": f"k{j}",
                            "label_ref_arxiv_id": f"2001.{j:05d}" if j < 6 else "", "source": "manual"})
        errors = self.work / "errors.csv"
        self.assertEqual(al.main(["--work", str(self.work), "--evaluate", str(manual), "--errors", str(errors)]), 0)
        self.assertEqual(list(csv.DictReader(open(errors, encoding="utf-8"))), [])  # all 6 found, nothing wrong


if __name__ == "__main__":
    unittest.main(verbosity=2)
