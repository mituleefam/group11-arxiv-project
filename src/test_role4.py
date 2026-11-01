"""Offline tests for Role 4. No real arXiv requests."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate import validate
from corpus_statistics import analyze
from benchmark import run
from benchmark_runs import aggregate
from per_paper_benchmark import evaluate_paper, timing_summary


class Role4OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        paper = self.root / "corpus" / "2511-21709"
        (paper / "v1" / "sections").mkdir(parents=True)
        (self.root / "work").mkdir()
        (paper / "v1" / "sections" / "intro.tex").write_text("Hello\\n\\\\begin{figure}X\\\\end{figure}", encoding="utf-8")
        record = {"arxiv_id": "2511.21709", "folder": "2511-21709", "status": "success",
                  "latest_version": 1, "versions": ["v1"]}
        with (self.root / "work" / "paper_manifest.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n" + json.dumps(record) + "\n")
        (paper / "metadata.json").write_text(json.dumps({
            "arxiv_id": "2511.21709", "title": "Test", "authors": ["Author"],
            "submission_date": "2025-11-17T00:00:00Z", "revised_dates": [], "venue": None,
            "versions": [{"version": "v1", "date": "2025-11-17T00:00:00Z"}]
        }), encoding="utf-8")
        (paper / "versions.json").write_text(json.dumps({
            "arxiv_id": "2511.21709", "versions": {"v1": {"status": "success",
            "kept_files": ["sections/intro.tex"], "original_bytes": 100, "kept_bytes": 36,
            "image_bytes": 70, "total_seconds": 3.0}}
        }), encoding="utf-8")
        refs = [{"ref_arxiv_id": "1706.03762", "ref_title": "Ref paper",
                 "ref_authors": ["A"], "ref_submission_date": None,
                 "ref_s2_id": "s2-test", "metadata_status": "complete"}]
        (paper / "references_all.json").write_text(json.dumps({
            "arxiv_id": "2511.21709", "status": "success", "arxiv_dates_complete": True, "references": refs
        }), encoding="utf-8")
        (paper / "references.json").write_text(json.dumps({
            "1706-03762": {"title": "Ref paper", "authors": ["A"],
                           "submission_date": None, "semantic_scholar_id": "s2-test"}
        }), encoding="utf-8")

    def test_stats_strict_pipeline_requires_arxiv_dates_complete(self):
        from corpus_statistics import analyze
        paper = self.root / "corpus" / "2511-21709"
        base = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(base["papers_pipeline_core_complete"], 1)
        self.assertEqual(base["papers_pipeline_complete_strict"], 1)
        path = paper / "references_all.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["arxiv_dates_complete"] = False
        path.write_text(json.dumps(doc), encoding="utf-8")
        measured = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(measured["papers_pipeline_core_complete"], 1)
        self.assertEqual(measured["papers_pipeline_complete_strict"], 0)
        self.assertEqual(measured["reference_papers_missing_arxiv_dates"], 1)

    def test_reference_subset_content_mismatch_is_detected(self):
        paper = self.root / "corpus" / "2511-21709"
        selected = json.loads((paper / "references.json").read_text(encoding="utf-8"))
        selected["1706-03762"]["submission_date"] = "2001-01-01T00:00:00Z"
        (paper / "references.json").write_text(json.dumps(selected), encoding="utf-8")
        issues = validate(self.root)
        self.assertTrue(any(i["check_name"] == "references_subset_content" and i["status"] == "FAIL"
                            for i in issues))

    def test_arxiv_date_pending_prevents_standalone_complete(self):
        paper = self.root / "corpus" / "2511-21709"
        references = json.loads((paper / "references_all.json").read_text(encoding="utf-8"))
        references["arxiv_dates_complete"] = False
        (paper / "references_all.json").write_text(json.dumps(references), encoding="utf-8")
        result = evaluate_paper(self.root, "2511.21709", 0)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["reference_complete"])
        issues = validate(self.root)
        self.assertTrue(any(i["check_name"] == "reference_dates" and i["status"] == "WARN" for i in issues))

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_data_passes_and_manifest_retries_not_double_counted(self):
        result = validate(self.root)
        self.assertFalse([r for r in result if r["status"] in {"FAIL", "PENDING"}], result)
        stats = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(stats["assigned_ids_discovered"], 1)
        self.assertEqual(stats["versions_expected"], 1)
        self.assertEqual(stats["papers_source_complete"], 1)
        self.assertEqual(stats["total_references_raw"], 1)

    def test_bad_extra_file_detected(self):
        (self.root / "corpus" / "2511-21709" / "v1" / "bad.png").write_bytes(b"image")
        result = validate(self.root)
        self.assertTrue(any(r["check_name"] == "source_types" and r["status"] == "FAIL" for r in result))

    def test_missing_references_reported_pending(self):
        (self.root / "corpus" / "2511-21709" / "references_all.json").unlink()
        self.assertTrue(any(r["check_name"] == "references" and r["status"] == "PENDING" for r in validate(self.root)))

    def _set_zero_byte_source(self, expected_hash=None):
        paper = self.root / "corpus" / "2511-21709"
        tex = paper / "v1" / "sections" / "intro.tex"
        tex.write_bytes(b"")
        path = paper / "versions.json"
        contents = json.loads(path.read_text(encoding="utf-8"))
        state = contents["versions"]["v1"]
        state["kept_bytes"] = 0
        if expected_hash is not None:
            state["sha256_by_file"] = {"sections/intro.tex": expected_hash}
        path.write_text(json.dumps(contents), encoding="utf-8")

    def test_zero_byte_without_archive_proof_warns_but_is_operationally_complete(self):
        self._set_zero_byte_source()
        issues = validate(self.root)
        self.assertTrue(any(r["check_name"] == "zero_byte_source" and r["status"] == "WARN" for r in issues))
        self.assertFalse([r for r in issues if r["status"] == "FAIL"], issues)
        stats = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(stats["papers_source_complete"], 1)
        self.assertEqual(stats["source_versions_with_unverified_zero_byte_files"], 1)

    def test_zero_byte_matching_archive_hash_passes(self):
        import hashlib
        self._set_zero_byte_source(hashlib.sha256(b"").hexdigest())
        issues = validate(self.root)
        self.assertTrue(any(r["check_name"] == "zero_byte_source" and r["status"] == "PASS" for r in issues))
        stats = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(stats["papers_source_complete"], 1)
        self.assertEqual(stats["source_versions_with_unverified_zero_byte_files"], 0)

    def test_zero_byte_conflicting_archive_hash_fails(self):
        import hashlib
        self._set_zero_byte_source(hashlib.sha256(b"actual original contents").hexdigest())
        issues = validate(self.root)
        self.assertTrue(any(r["check_name"] == "zero_byte_source" and r["status"] == "FAIL" for r in issues))
        stats = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(stats["papers_source_complete"], 0)
        self.assertEqual(stats["source_versions_with_zero_byte_hash_mismatch"], 1)

    def test_zero_byte_with_wrong_kept_bytes_is_invalid(self):
        paper = self.root / "corpus" / "2511-21709"
        (paper / "v1" / "sections" / "intro.tex").write_bytes(b"")
        issues = validate(self.root)
        self.assertTrue(any(r["check_name"] == "kept_byte_count" and r["status"] == "FAIL" for r in issues))
        stats = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(stats["papers_source_complete"], 0)

    def test_missing_assigned_id_detected(self):
        result = validate(self.root, expected_ids={"2511.21709", "2511.21710"})
        self.assertTrue(any(r["check_name"] == "assigned_id_missing" and r["arxiv_id"] == "2511.21710" for r in result))

    def test_leftover_tmp_detected_only_after_run(self):
        tmp = self.root / "corpus" / "2511-21709" / ".v1.tmp"
        tmp.mkdir()
        (tmp / "extra.tex").write_bytes(b"partial")
        assert not any(r["check_name"] == "leftover_tmp" for r in validate(self.root, finished=False))
        self.assertTrue(any(r["check_name"] == "leftover_tmp" for r in validate(self.root, finished=True)))

    def test_statistics_exclude_missing_kept_file(self):
        tex = self.root / "corpus" / "2511-21709" / "v1" / "sections" / "intro.tex"
        tex.unlink()
        stats = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(stats["papers_source_complete"], 0)
        self.assertEqual(stats["source_versions_with_invalid_kept_files"], 1)

    def test_reference_statistics_separate_denominators(self):
        stats = {r["metric"]: r["value"] for r in analyze(self.root, assigned_ids={"2511.21709", "2511.21710"})}
        self.assertEqual(stats["reference_collection_success_rate_found"], 1.0)
        self.assertEqual(stats["references_per_assigned_id_observed_lower_bound"], 0.5)

    def test_verified_404_is_warning_only_when_reviewed(self):
        state = self.root / "corpus" / "2511-21709" / "versions.json"
        raw = json.loads(state.read_text(encoding="utf-8"))
        raw["versions"]["v1"]["status"] = "not_found"
        state.write_text(json.dumps(raw), encoding="utf-8")
        unreviewed = validate(self.root)
        reviewed = validate(self.root, verified_404={("2511.21709", "v1")})
        self.assertTrue(any(r["check_name"] == "version" and r["status"] == "PENDING" for r in unreviewed))
        self.assertTrue(any(r["check_name"] == "version" and r["status"] == "WARN" for r in reviewed))

    def test_discovery_total_logged_seconds(self):
        path = self.root / "logs" / "run_log.csv"
        path.parent.mkdir(exist_ok=True)
        path.write_text("run_id,stage,seconds\nA,discovery,3.5\nB,discovery,4.0\nA,download,100\n")
        stats = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(stats["discovery_total_logged_stage_seconds"], 7.5)
        self.assertEqual(stats["discovery_logged_runs"], 2)

    def test_recorded_kept_files_exact_set_is_verified(self):
        extra = self.root / "corpus" / "2511-21709" / "v1" / "extra.tex"
        extra.write_text("extra", encoding="utf-8")
        checks = validate(self.root)
        self.assertTrue(any(c["check_name"] == "kept_extra" and c["status"] == "FAIL" for c in checks))
        stats = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(stats["papers_source_complete"], 0)

    def test_dangerous_kept_paths_are_rejected(self):
        versions = self.root / "corpus" / "2511-21709" / "versions.json"
        doc = json.loads(versions.read_text())
        doc["versions"]["v1"]["kept_files"] = ["../secrets.tex"]
        versions.write_text(json.dumps(doc))
        result = validate(self.root)
        self.assertTrue(any(r["check_name"] == "kept_paths" and r["status"] == "FAIL" for r in result))

    def test_references_with_error_are_excluded_from_complete_means(self):
        path = self.root / "corpus" / "2511-21709" / "references_all.json"
        doc = json.loads(path.read_text())
        doc["status"] = "api_error"
        path.write_text(json.dumps(doc))
        stats = {r["metric"]: r["value"] for r in analyze(self.root)}
        self.assertEqual(stats["references_papers_with_doc"], 0)
        self.assertEqual(stats["total_references_raw"], 0)
        self.assertEqual(stats["reference_collection_success_rate_found"], 0)

    def test_aggregate_two_benchmark_invocations(self):
        log = self.root / "logs"
        log.mkdir(exist_ok=True)
        for name, wall, peak in [("A", 10, 100), ("B", 20, 200)]:
            (log / f"performance_summary_{name}.json").write_text(json.dumps({
                "run_id": name, "wall_seconds": wall, "return_code": 0,
                "interrupted": False, "ram_sampled_peak_bytes": peak,
                "data_dir_sampled_peak_bytes": peak * 2
            }))
            (log / f"performance_samples_{name}.csv").write_text(
                "rss_tree_bytes\n" + str(peak) + "\n")
        rows, data = aggregate(log)
        self.assertEqual(len(rows), 2)
        self.assertEqual(data["sum_measured_invocation_wall_seconds"], 30)
        self.assertEqual(data["highest_sampled_process_rss_peak_bytes"], 200)
        self.assertEqual(data["mean_sampled_rss_bytes_across_available_rows"], 150)

    def test_paper_timing_summary_excludes_incomplete_runs(self):
        rows = [{"wall_seconds": 3.0, "status": "complete"},
                {"wall_seconds": 5.0, "status": "complete"},
                {"wall_seconds": 100, "status": "incomplete"}]
        result = timing_summary(rows)
        self.assertEqual(result["sample_complete"], 2)
        self.assertEqual(result["mean_seconds_per_complete_sample_paper"], 4.0)

    def test_standalone_paper_completeness_requires_metadata_versions_and_refs(self):
        result = evaluate_paper(self.root, "2511.21709", 0)
        self.assertEqual(result["status"], "complete")
        (self.root / "corpus/2511-21709/references_all.json").unlink()
        self.assertEqual(evaluate_paper(self.root, "2511.21709", 0)["status"], "incomplete")

    def test_stats_import_standalone_sample_without_claiming_bulk_latency(self):
        p = self.root / "paper_timings.csv"
        p.write_text("arxiv_id,wall_seconds,status\n2511.21709,8,complete\n2511.21710,2,incomplete\n")
        stats = {r["metric"]: r["value"] for r in analyze(self.root, paper_timings=p)}
        self.assertEqual(stats["standalone_paper_complete_sample_count"], 1)
        self.assertEqual(stats["standalone_paper_sample_mean_seconds"], 8.0)

    def test_validate_dev_sample_preserves_full_data(self):
        self.assertEqual(validate(self.root, max_papers=1), validate(self.root))

    def test_validate_progress_does_not_modify_statuses(self):
        old = validate(self.root)
        from contextlib import redirect_stdout
        from io import StringIO
        with redirect_stdout(StringIO()) as buffer:
            new = validate(self.root, progress_every=1)
        self.assertEqual(old, new)
        self.assertIn("checking paper", buffer.getvalue())

    def test_orphaned_benchmark_heartbeat_not_added_to_confirmed_total(self):
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "performance_progress_crashed.json").write_text(json.dumps({
            "run_id": "crashed", "observed_elapsed_seconds": 20, "last_observed_utc": "2026-10-23T00:00:00Z"
        }))
        _, report = aggregate(logs)
        self.assertEqual(report["orphaned_incomplete_invocation_count"], 1)
        self.assertEqual(report["sum_measured_invocation_wall_seconds"], 0)

    def test_benchmark_wrapper_runs_offline_process(self):
        try:
            import psutil  # noqa
        except ImportError:
            self.skipTest("psutil not installed")
        out = self.root / "logs"
        rc = run([sys.executable, "-c", "import time; time.sleep(0.15)"], self.root, out, 0.04, 0.08)
        self.assertEqual(rc, 0)
        summaries = list(out.glob("performance_summary_*.json"))
        self.assertEqual(len(summaries), 1)
        self.assertGreater(json.loads(summaries[0].read_text())["wall_seconds"], 0)
        progress = list(out.glob("performance_progress_*.json"))
        self.assertEqual(len(progress), 1)
        self.assertEqual(json.loads(progress[0].read_text())["status"], "finished")


if __name__ == "__main__":
    unittest.main()
