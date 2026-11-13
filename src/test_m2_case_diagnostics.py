"""Regression tests for evidence-based diagnosis of the five M2 warning categories."""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnose_m2_warnings import analyze, run, _cited_keys


class CaseDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paper = self.root / 'corpus' / '2604-07041'
        (self.paper / 'parsed').mkdir(parents=True)
        (self.paper / 'v1').mkdir()
        self.write('elements.json', {'arxiv_id': '2604.07041', 'columns': ['element_id', 'kind', 'content'],
                                    'rows': [['2604-07041#d1', 'document', 'Version 1 root']]})
        self.write('hierarchy.json', {'arxiv_id': '2604.07041', 'columns': ['version', 'element_id', 'parent_id'],
                                     'rows': []})
        self.write('bibtex.json', {'arxiv_id': '2604.07041', 'rows': []})
        self.write('gathering.json', {'v1': {'status': 'ok', 'root': 'main.tex',
                                            'files': ['main.tex'], 'missing': [], 'problems': []}})
        (self.paper / 'v1' / 'main.tex').write_text('\\documentclass{article}\n\\begin{document}\n'
                                                     'This is ordinary meaningful text.\\end{document}',
                                                     encoding='utf-8')

    def write(self, name, content):
        (self.paper / 'parsed' / name).write_text(json.dumps(content), encoding='utf-8')

    def row(self, check, version='v1'):
        return {'arxiv_id': '2604.07041', 'version': version, 'check_name': check,
                'status': 'WARN', 'details': 'count=1'}

    def evidence(self, result):
        return json.loads(result['evidence'])

    def test_empty_tree_flags_real_source_content(self):
        result = analyze(self.row('empty_tex_tree'), self.root)
        self.assertEqual(result['classification'], 'content_present_but_no_hierarchy_edges')
        self.assertGreater(self.evidence(result)['standalone_leaves'], 0)

    def test_multiple_documentclasses_is_informational_not_automatically_fixed(self):
        self.write('gathering.json', {'v1': {'status': 'ok', 'root': 'main.tex', 'files': ['main.tex'],
                     'missing': [], 'unused': ['supp.tex'],
                     'problems': ['2 files with \\documentclass: main.tex taken (others: supp.tex)']}})
        result = analyze(self.row('gathering_problems'), self.root)
        self.assertEqual(result['classification'], 'multiple_root_candidates')
        self.assertEqual(result['priority'], 'info')
        self.assertEqual(result['status'], 'WARN')

    def test_gathering_missing_file_is_high_priority(self):
        self.write('gathering.json', {'v1': {'status': 'ok', 'root': 'main.tex',
                                           'missing': ['section.tex'], 'problems': ['missing input']}})
        result = analyze(self.row('gathering_problems'), self.root)
        self.assertEqual(result['classification'], 'missing_source_dependency')
        self.assertEqual(result['priority'], 'high')

    def test_bib_source_entry_not_exported(self):
        self.write('elements.json', {'rows': [['p#s1', 'sentence', r'We cite \\citep{ReferenceKey}.']]})
        (self.paper / 'v1' / 'refs.bib').write_text('@article{ReferenceKey, title={A Work}, year={2020}}', encoding='utf-8')
        result = analyze(self.row('citation_key_coverage', ''), self.root)
        self.assertEqual(result['classification'], 'source_defined_missing_from_output')
        self.assertEqual(self.evidence(result)['source_defined_but_not_exported'], ['ReferenceKey'])

    def test_citation_case_alias_is_detected_without_changing_output(self):
        self.write('elements.json', {'rows': [['p#s1', 'sentence', r'\\Cite{Zhang2025}.']]})
        self.write('bibtex.json', {'rows': [{'citation_key': 'zhang2025'}]})
        result = analyze(self.row('citation_key_coverage', ''), self.root)
        self.assertEqual(result['classification'], 'citation_case_or_alias_mismatch')
        self.assertEqual(self.evidence(result)['case_only_in_table'], {'Zhang2025': ['zhang2025']})
        self.assertEqual(_cited_keys({'rows': [['x', 'sentence', r'\\nocite{*}']]}), set())

    def test_nonstandard_leaf_evidence_contains_kind_and_content(self):
        self.write('elements.json', {'rows': [['p#d', 'document', 'Version 1 root'],
                                            ['p#h', 'heading', 'Introduction']]})
        self.write('hierarchy.json', {'rows': [[1, 'p#h', 'p#d']]})
        result = analyze(self.row('nonstandard_leaf_kinds', '1'), self.root)
        self.assertEqual(self.evidence(result)['kinds'], {'heading': 1})
        self.assertEqual(result['priority'], 'review')

    def test_encoding_reports_literal_replacement_character_from_source(self):
        self.write('elements.json', {'rows': [['p#s1', 'sentence', 'A replacement \ufffd appears.']]})
        (self.paper / 'v1' / 'main.tex').write_text('Original \ufffd source', encoding='utf-8')
        result = analyze(self.row('encoding_replacement_chars', ''), self.root)
        self.assertEqual(result['classification'], 'replacement_character_present_in_source')
        self.assertTrue(self.evidence(result)['source_files_containing_U+FFFD'])

    def test_validator_uses_shared_citation_grammar(self):
        from validate_m2 import Reporter, validate_paper
        self.write('elements.json', {'arxiv_id': '2604.07041',
             'columns': ['element_id', 'kind', 'content'],
             'rows': [['2604-07041#d1', 'document', 'Version 1 root'],
                      ['2604-07041#s1', 'sentence', r'\Cite{MissingKey} and \nocite{*}.']]})
        self.write('hierarchy.json', {'arxiv_id': '2604.07041',
             'columns': ['version', 'element_id', 'parent_id'],
             'rows': [[1, '2604-07041#s1', '2604-07041#d1']]})
        reporter = Reporter()
        validate_paper({'arxiv_id': '2604.07041', 'paper_dir': str(self.paper)}, reporter)
        rows = [r for r in reporter.rows if r['check_name'] == 'citation_key_coverage']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'WARN')
        self.assertIn('matched=0/1', rows[0]['details'])
        self.assertNotIn("'*'", rows[0]['details'])

    def test_produces_reports_without_editing_corpus(self):
        log = self.root / 'logs'
        log.mkdir()
        with (log / 'validation_report_m2.csv').open('w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['arxiv_id', 'version', 'check_name', 'status', 'details'])
            w.writeheader()
            w.writerow(self.row('empty_tex_tree'))
            w.writerow({'arxiv_id': '2604.07041', 'version': '1', 'check_name': 'cycle_free', 'status': 'PASS', 'details': ''})
        result = run(self.root)
        self.assertEqual(result['analyzed_checks'], 1)
        self.assertTrue((log / 'warning_evidence_m2.csv').is_file())
        self.assertTrue((log / 'warning_evidence_summary_m2.json').is_file())
        self.assertFalse((self.paper / 'parsed' / 'processed.json').exists())


if __name__ == '__main__':
    unittest.main()
