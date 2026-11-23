"""Regression tests for post-run warning audit (no external dependencies)."""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_m2_warnings import classify, run


class WarningAuditTests(unittest.TestCase):
    def test_severity_and_coverage(self):
        root = Path("/unused")
        case = {"arxiv_id": "2604.07041", "version": "", "status": "WARN",
                "check_name": "citation_key_coverage",
                "details": "matched=0/48, missing=['a']"}
        result = classify(case, root)
        self.assertEqual(result["priority"], "high")
        self.assertEqual(result["citation_total"], "48")
        case["details"] = "matched=40/41, missing=['a']"
        self.assertEqual(classify(case, root)["priority"], "review")
        case.update(check_name="empty_tex_tree", details="TeX has no edges")
        self.assertEqual(classify(case, root)["priority"], "high")

    def test_gathering_details_distinguish_multiple_roots(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            dest = root / "corpus" / "2604-07442" / "parsed"
            dest.mkdir(parents=True)
            (dest / "gathering.json").write_text(json.dumps({"v1": {
                "problems": ["2 files with \\documentclass: main.tex taken (others: supp.tex)"]
            }}), encoding="utf-8")
            base = {"arxiv_id": "2604.07442", "version": "v1", "status": "WARN",
                    "check_name": "gathering_problems", "details": "count=1"}
            self.assertEqual(classify(base, root)["priority"], "info")
            (dest / "gathering.json").write_text(json.dumps({"v1": {
                "problems": ["Could not read included file: x.tex"]
            }}), encoding="utf-8")
            self.assertEqual(classify(base, root)["priority"], "review")

    def test_report_preserves_original_status_and_counts(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            logs = root / "logs"
            logs.mkdir()
            with (logs / "validation_report_m2.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["arxiv_id", "version", "check_name", "status", "details"])
                w.writeheader()
                w.writerows([
                    {"arxiv_id":"p1","version":"v1","check_name":"cycle_free","status":"PASS","details":""},
                    {"arxiv_id":"p1","version":"v1","check_name":"empty_tex_tree","status":"WARN","details":""},
                    {"arxiv_id":"p2","version":"v1","check_name":"cycle_free","status":"FAIL","details":""},
                ])
            res = run(root)
            self.assertEqual(res["checks_by_status"], {"PASS": 1, "WARN": 1, "FAIL": 1})
            self.assertEqual(res["papers_with_nonpass_checks"], 2)
            self.assertTrue((logs / "warning_triage_m2.csv").is_file())
            self.assertTrue((logs / "warning_summary_m2.json").is_file())
            with (logs / "warning_triage_m2.csv").open(encoding="utf-8-sig",newline="") as f:
                self.assertEqual([r["status"] for r in csv.DictReader(f)], ["WARN", "FAIL"])

if __name__ == "__main__":
    unittest.main()
