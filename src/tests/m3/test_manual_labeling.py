"""Tests of the labelling steps 1 and 2 (select_manual.py, label_sheet.py); no network, a few seconds.

    python src/tests/m3/test_manual_labeling.py
"""

from __future__ import annotations

import csv
import json
import random
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parents[2] / "m3"  # src/m3/
sys.path.insert(0, str(HERE))
import label_sheet  # noqa: E402
import select_manual  # noqa: E402


def entry(pid, key, title, authors=(), year=2020, doi=None, arxiv=None):
    return {"arxiv_id": pid, "citation_key": key, "entry_type": "article", "title": title, "authors": list(authors),
            "journal": None, "url": None, "title_clean": title.lower() if title else None,
            "title_tokens": [], "authors_last": [a.lower() for a in authors],
            "first_author_last": authors[0].lower() if authors else None, "year": year, "doi": doi,
            "arxiv_id_in_entry": arxiv, "arxiv_id_from": "url" if arxiv else None}


def ref(pid, rid, title, authors=(), year=2020, doi=None):
    return {"arxiv_id": pid, "ref_arxiv_id": rid, "ref_title": title, "ref_authors": list(authors),
            "ref_submission_date": None, "ref_s2_id": None, "title_clean": title.lower(), "title_tokens": [],
            "authors_last": [a.lower() for a in authors], "first_author_last": None, "year": year,
            "year_s2": year, "doi": doi, "venue": None}


class Fixture(unittest.TestCase):
    """A work folder with publications of the Bao range: 2604.05600 (easy), 2604.05601 (hard), ..."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work, self.m1 = self.tmp / "work", self.tmp / "m1"
        self.work.mkdir()
        entries, refs, stats, status = [], [], [], []
        rng = random.Random(0)
        for i in range(30):
            pid = f"2604.{5600 + i:05d}"
            m, n = 30 + i % 5, 30
            matched = [m, m // 2, 11 + i % 2][i % 3]  # easy, medium, hard
            for j in range(n):
                refs.append(ref(pid, f"2001.{j:05d}", f"reference title {j}", ["Smith"]))
            for j in range(m):
                entries.append(entry(pid, f"k{j}", f"reference title {j % n}" if j < matched else f"other {j}",
                                     ["Smith"], arxiv=None))
            stats.append({"arxiv_id": pid, "m": m, "n": n, "pairs": m * n, "entries_with_arxiv_id": 0,
                          "arxiv_id_hits": 0, "entries_with_doi": 0, "doi_hits": 0, "entries_without_title": 0,
                          "misc_entries": 0})
            status.append({"arxiv_id": pid, "status": "success" if i != 29 else "empty", "refs_total": n,
                           "refs_arxiv": n, "source": "references_all.json"})
            folder = self.m1 / "corpus" / pid.replace(".", "-")
            folder.mkdir(parents=True)
            cat = "cs.LG" if i % 4 else "math.AP"
            (folder / "metadata.json").write_text(json.dumps({"primary_category": cat}), encoding="utf-8")
        rng.shuffle(entries)
        pq.write_table(pa.Table.from_pylist(entries), self.work / "entries_clean.parquet")
        pq.write_table(pa.Table.from_pylist(refs), self.work / "references_clean.parquet")
        for name, rows in (("paper_stats.csv", stats), ("papers_refs.csv", status)):
            with open(self.work / name, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def select(self, *extra):
        out = self.work / "manual_pick.csv"
        code = select_manual.main(["--work", str(self.work), "--data-dir", str(self.m1), "--out", str(out), *extra])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))


class SelectTests(Fixture):
    def test_range_of(self):
        self.assertEqual(select_manual.range_of("2604.05535"), "Bao")
        self.assertEqual(select_manual.range_of("2604.10534"), "Bao")
        self.assertIsNone(select_manual.range_of("2604.10535"))
        self.assertEqual(select_manual.range_of("2511.21709"), "Tram")
        self.assertIsNone(select_manual.range_of("hep-th/9901001"))

    def test_levels_and_determinism(self):
        picks = self.select()
        self.assertEqual(len(picks), 5)
        self.assertEqual(sorted(p["difficulty"] for p in picks), ["easy", "hard", "hard", "medium", "medium"])
        self.assertEqual([p["arxiv_id"] for p in picks], [p["arxiv_id"] for p in self.select()])
        self.assertTrue(any(not p["primary_category"].startswith("cs.") for p in picks))
        self.assertNotIn("2604.05629", [p["arxiv_id"] for p in picks])  # Semantic Scholar status not success

    def test_filters_and_exclude(self):
        picks = self.select("--min-m", "33")
        self.assertTrue(all(int(p["m"]) >= 33 for p in picks))
        self.assertEqual(self.select("--min-sure", "40"), [])  # no paper reaches 40 sure pairs
        self.assertTrue(all(int(p["sure_pairs"]) >= 10 for p in self.select()))
        first = [p["arxiv_id"] for p in self.select()]
        excl = self.tmp / "used.txt"
        excl.write_text("\n".join(first), encoding="utf-8")
        self.assertFalse(set(first) & {p["arxiv_id"] for p in self.select("--exclude", str(excl))})


class SheetTests(Fixture):
    def export(self):
        picks = self.select()
        out = self.work / "sheet.csv"
        args = Namespace(work=self.work, picks=self.work / "manual_pick.csv", labeler="T", range=None, out=out)
        rows, ref_rows = label_sheet.build_rows(self.work, [p["arxiv_id"] for p in picks])
        self.assertEqual(label_sheet.cmd_export(args), 0)
        return picks, rows, ref_rows

    def test_export_rows(self):
        picks, rows, ref_rows = self.export()
        self.assertEqual(len(rows), sum(int(p["m"]) for p in picks))  # every entry of every pick
        self.assertEqual(len(ref_rows), 30 * len(picks))
        hinted = [r for r in rows if r["rule_hint"]]
        self.assertTrue(hinted)
        for r in hinted:  # an exact title match is the first suggestion
            self.assertEqual(r["cand_1_ref_arxiv_id"], r["rule_hint"].split("-> ")[1])
        self.assertTrue(all(r["label_ref_arxiv_id"] == "" and r["checked"] == "" for r in rows))
        self.assertEqual(label_sheet.natural_key("2") < label_sheet.natural_key("10"), True)

    def test_import_checks(self):
        picks, rows, _ = self.export()
        refs = {p["arxiv_id"]: {f"2001.{j:05d}" for j in range(30)} for p in picks}
        labels, problems = label_sheet.check(rows, refs, 20)
        self.assertEqual(labels, [])
        self.assertTrue(any("not checked" in p for p in problems))
        for r in rows:
            r["checked"] = "x"
        rows[0]["label_ref_arxiv_id"] = "9999.99999"
        labels, problems = label_sheet.check(rows, refs, 20)
        self.assertTrue(any("is not a reference" in p for p in problems))
        for r in rows:  # follow the hints: some publications stay below 20 matched pairs
            r["label_ref_arxiv_id"] = r["rule_hint"].split("-> ")[1] if r["rule_hint"] else ""
        labels, problems = label_sheet.check(rows, refs, 20)
        per_paper = {p["arxiv_id"]: sum(1 for r in rows if r["arxiv_id"] == p["arxiv_id"] and r["label_ref_arxiv_id"])
                     for p in picks}
        self.assertEqual(sum("matched pairs" in p for p in problems), sum(n < 20 for n in per_paper.values()))
        labels, problems = label_sheet.check(rows, refs, 10)
        self.assertEqual(problems, [])
        self.assertEqual(len(labels), len(rows))  # "no match" decisions are kept too
        labels, problems = label_sheet.check(rows + [dict(rows[0])], refs, 10)
        self.assertTrue(any("2 rows" in p for p in problems))
        few = [r for r in rows if r["arxiv_id"] == picks[0]["arxiv_id"]][:5]
        self.assertTrue(any("at least 20" in p for p in label_sheet.check(few, refs, 20)[1]))

    def test_import_writes_labels(self):
        picks, rows, _ = self.export()
        sheet = self.work / "filled.csv"
        with open(sheet, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=label_sheet.SHEET_COLS)
            w.writeheader()
            for r in rows:
                w.writerow({**r, "checked": "x", "label_ref_arxiv_id": r["rule_hint"].split("-> ")[1]
                            if r["rule_hint"] else ""})
        out = self.work / "labels.csv"
        args = Namespace(work=self.work, sheet=sheet, labeler="T", min_labelled=10, out=out)
        self.assertEqual(label_sheet.cmd_import(args), 0)
        with open(out, encoding="utf-8", newline="") as f:
            labels = list(csv.DictReader(f))
        self.assertEqual(list(labels[0]), label_sheet.LABEL_COLS)
        self.assertEqual({x["source"] for x in labels}, {"manual"})
        self.assertEqual(len(labels), len(rows))
        for x in labels:
            self.assertEqual(x["is_match"], "true" if x["label_ref_arxiv_id"] else "false")


if __name__ == "__main__":
    unittest.main(verbosity=2)
