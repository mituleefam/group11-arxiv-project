"""Member 4 regression tests: exact ID contract, graphs, validation and stats.

Run from repo root:
    python -m unittest discover -s src -p test_m2_member4.py -v
No actual corpus or network is required. Parquet tests are conditional on
pyarrow being installed and are additionally exercised on Colab.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dedup_elements import Registry
from benchmark_m2 import inspect_completion
from statistics_m2 import collect
from validate_m2 import Reporter, validate_paper, validate_parquet

PAPER = "2604.05535"
PREFIX = "2604-05535"


def fixture(root: Path) -> dict:
    paper_dir = root / "corpus" / PREFIX
    parsed = paper_dir / "parsed"
    parsed.mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (paper_dir / "v1").mkdir()
    (paper_dir / "v2").mkdir()
    (paper_dir / "v1" / "main.tex").write_text(r"\documentclass{article}\begin{document}\section{Intro} A.\end{document}", encoding="utf-8")
    (paper_dir / "v2" / "main.tex").write_text(r"\documentclass{article}\begin{document}\section{Intro} A.\end{document}", encoding="utf-8")
    reg = Registry(PAPER)
    d1 = reg.element_id("document", "Version 1 root", None, 0)
    d2 = reg.element_id("document", "Version 2 root", None, 0)
    h = reg.element_id("heading", "Intro", None, 0)
    s = reg.element_id("sentence", "Alpha is useful.", h, 0)
    t = reg.element_id("sentence", "See \\cite{key1}.", h, 0)
    e = reg.rows()
    (parsed / "elements.json").write_text(json.dumps({"arxiv_id": PAPER, "columns": ["element_id", "kind", "content"],
        "rows": [[x["element_id"], x["kind"], x["content"]] for x in e]}), encoding="utf-8")
    (parsed / "hierarchy.json").write_text(json.dumps({"arxiv_id": PAPER, "columns": ["version", "element_id", "parent_id"],
        "rows": [[1,h,d1],[1,s,h],[1,t,h],[2,h,d2],[2,s,h]]}), encoding="utf-8")
    (parsed / "bibtex.json").write_text(json.dumps({"arxiv_id":PAPER,"rows":[{
        "arxiv_id":PAPER,"citation_key":"key1","entry_type":"article","title":"Title",
        "authors":["Doe, J"],"year":2025,"doi":None,"journal":None,"url":None}]}), encoding="utf-8")
    (parsed / "gathering.json").write_text(json.dumps({
        "v1":{"status":"ok", "files":["main.tex"],"problems":[]},
        "v2":{"status":"ok", "files":["main.tex"],"problems":[]}}), encoding="utf-8")
    (root / "logs" / "parse_manifest.jsonl").write_text(json.dumps({"arxiv_id":PAPER,"status":"done","seconds":1.5})+"\n")
    return {"arxiv_id":PAPER,"paper_dir":str(paper_dir),"versions":["v1","v2"]}


class RegistryTests(unittest.TestCase):
    def test_leaf_dedup_across_parents_versions_and_whitespace(self):
        r = Registry(PAPER)
        a = r.element_id("sentence", "  Alpha\n beta.  ", "p1", 0)
        b = r.element_id("sentence", "Alpha beta.", "p2", 1)
        self.assertEqual(a,b)
        self.assertEqual(r.rows()[0]["content"], "Alpha beta.")
        self.assertEqual(len(r.rows()), 1)

    def test_kind_and_paper_scoping(self):
        r = Registry(PAPER)
        a = r.element_id("sentence", "a", None, 0)
        b = r.element_id("formula", "a", None, 0)
        c = Registry("2604.05536").element_id("sentence", "a", None, 0)
        self.assertNotEqual(a,b)
        self.assertNotEqual(a,c)

    def test_heading_repeats_and_version_roots(self):
        r = Registry(PAPER)
        x = r.element_id("heading", "Results", None, 0)
        self.assertEqual(x, r.element_id("heading", "Results", None, 0))
        self.assertNotEqual(x, r.element_id("heading", "Results", None, 1))
        self.assertNotEqual(r.element_id("document", "Version 1 root", None, 0),
                            r.element_id("document", "Version 2 root", None, 0))

    def test_structural_node_parent_sensitive(self):
        r = Registry(PAPER)
        a = r.element_id("item", "", "p1", 0)
        b = r.element_id("item", "", "p2", 0)
        self.assertNotEqual(a, b)

    def test_bad_inputs_and_defensive_rows(self):
        with self.assertRaises(ValueError): Registry("")
        r = Registry(PAPER)
        with self.assertRaises(TypeError): r.element_id("sentence", None, None, 0)
        with self.assertRaises(ValueError): r.element_id("sentence", "A", None, -1)
        r.element_id("sentence", "A", None, 0)
        rows = r.rows()
        rows[0]["content"] = "MUTATED"
        self.assertEqual(r.rows()[0]["content"], "A")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paper = fixture(self.root)

    def _statuses(self):
        report = Reporter()
        validate_paper(self.paper, report)
        grouped = {}
        for row in report.rows:
            name, status = row["check_name"], row["status"]
            previous = grouped.get(name, "PASS")
            grouped[name] = "FAIL" if "FAIL" in (previous, status) else (
                "WARN" if "WARN" in (previous, status) else "PASS")
        return grouped

    def test_good_json_graph_and_bibtex(self):
        statuses = self._statuses()
        for key in ("elements_rows", "bibtex_rows", "hierarchy_rows", "version_coverage",
                    "one_document_root", "cycle_free", "root_reachability",
                    "edge_elements_exist", "citation_key_coverage", "bibliography_exclusion"):
            self.assertEqual(statuses[key], "PASS", key)

    def test_cycle_and_unreachable_detected(self):
        file = Path(self.paper["paper_dir"]) / "parsed" / "hierarchy.json"
        value = json.loads(file.read_text())
        value["rows"].append([1, value["rows"][0][2], value["rows"][0][1]])
        file.write_text(json.dumps(value))
        statuses = self._statuses()
        self.assertEqual(statuses["cycle_free"], "FAIL")
        self.assertEqual(statuses["root_not_child"], "FAIL")

    def test_missing_parent_detected(self):
        file = Path(self.paper["paper_dir"]) / "parsed" / "hierarchy.json"
        value = json.loads(file.read_text())
        value["rows"].append([1,value["rows"][1][1],"missing#h1"])
        file.write_text(json.dumps(value))
        self.assertEqual(self._statuses()["edge_elements_exist"], "FAIL")

    def test_extra_bibliography_block_detected(self):
        file = Path(self.paper["paper_dir"]) / "parsed" / "elements.json"
        value = json.loads(file.read_text())
        value["rows"][3][2] = r"\begin{thebibliography}{9}"
        file.write_text(json.dumps(value))
        self.assertEqual(self._statuses()["bibliography_exclusion"], "FAIL")

    def test_stats_element_occurrences_and_versions(self):
        rows = collect(self.root)
        summary = {r["metric"]: r["value"] for r in rows}
        self.assertEqual(summary["elements_occurrences_before_dedup"], 7)
        self.assertEqual(summary["elements_unique_after_dedup"], 5)
        self.assertAlmostEqual(summary["element_reduction_percent"], 100*2/7, places=3)
        self.assertEqual(summary["versions_with_tex"],2)
        self.assertEqual(summary["bibtex_entries_before_dedup"],"")

    def test_integration_m1_m3_registry_with_pdf_only(self):
        """Execute the REAL Member 1 pipeline with the Member 4 registry."""
        paper_dir = Path(self.paper["paper_dir"])
        shutil.rmtree(paper_dir / "parsed")
        (paper_dir / "versions.json").write_text(json.dumps({"versions": {
            "v1": {"status": "success"},
            "v2": {"status": "success"},
            "v3": {"status": "pdf_only"}}}), encoding="utf-8")
        project = Path(__file__).resolve().parent
        proc = subprocess.run([sys.executable, str(project / "pipeline_m2.py"),
                               "--data-dir", str(self.root), "--workers", "1",
                               "--force", "--no-export"], capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("stand-in for dedup_elements.Registry", proc.stdout)
        report = Reporter()
        counts = validate_paper(self.paper, report)
        self.assertEqual(counts["versions"], 3)
        self.assertEqual(sum(r["status"] == "FAIL" for r in report.rows), 0, report.rows)
        table = json.loads((paper_dir / "parsed" / "elements.json").read_text())
        self.assertEqual(sum(row[1] == "document" for row in table["rows"]), 3)

    def test_benchmark_detects_per_paper_failure(self):
        total, failures = inspect_completion(self.root, 0)
        self.assertEqual(total, 1)
        self.assertEqual(failures, [])
        with (self.root / "logs" / "parse_manifest.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"arxiv_id": PAPER, "status": "failed",
                                     "error": "synthetic worker failure"}) + "\n")
        total, failures = inspect_completion(self.root, 0)
        self.assertEqual(total, 1)
        self.assertEqual(failures, [PAPER])

    @unittest.skipUnless(__import__("importlib").util.find_spec("pyarrow"), "pyarrow absent")
    def test_parquet_schema_if_available(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from validate_m2 import expected_arrow_schemas
        root = self.root / "tables"
        for name,schema in expected_arrow_schemas().items():
            folder = root / name
            folder.mkdir(parents=True)
            table = pa.Table.from_batches([], schema=schema)
            pq.write_table(table, folder / "part-00000.parquet")
        report = Reporter()
        self.assertEqual(validate_parquet(root, report, required=True),
                         {"elements":0,"hierarchy":0,"bibtex":0})
        self.assertFalse(any(r["status"] == "FAIL" for r in report.rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
