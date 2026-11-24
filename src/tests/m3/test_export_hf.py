"""Tests of src/m3/export_hf.py and src/m3/validate_m3.py on small fake data; no network, a few seconds.

    python src/tests/m3/test_export_hf.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SRC = Path(__file__).resolve().parents[2]  # src/
sys.path.insert(0, str(SRC / "m3"))
sys.path.insert(0, str(SRC))
import export_hf  # noqa: E402
import snapshot  # noqa: E402
import validate_m3  # noqa: E402

IN_RANGE = ["2604.05535", "2604.05536"]
OUT_OF_RANGE = "2605.00001"


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def make_m1(data_dir: Path, title_suffix: str = "") -> None:
    for i, pid in enumerate(IN_RANGE + [OUT_OF_RANGE]):
        folder = data_dir / "corpus" / pid.replace(".", "-")
        write_json(folder / "metadata.json", {"arxiv_id": pid, "title": f"Paper {i}{title_suffix}",
                                              "authors": ["Ann Author", "Bob Writer"],
                                              "submission_date": "2026-04-07T07:35:45Z", "revised_dates": [],
                                              "venue": None, "primary_category": "cs.LG", "categories": ["cs.LG"],
                                              "versions": ["v1"]})
        write_json(folder / "references_all.json", {
            "arxiv_id": pid, "status": "success",
            "references": [{"ref_arxiv_id": "1706.03762", "ref_title": "Attention is all you need",
                            "ref_authors": ["Ashish Vaswani"], "ref_submission_date": "2017-06-12",
                            "ref_s2_id": "s2a"},
                           {"ref_arxiv_id": "1810.04805", "ref_title": "BERT", "ref_authors": ["Jacob Devlin"],
                            "ref_submission_date": "2018-10-11", "ref_s2_id": "s2b"},
                           {"ref_arxiv_id": None, "ref_title": "A book", "ref_authors": [], "ref_s2_id": "s2c"}]})
        (folder / "v1").mkdir(exist_ok=True)
        (folder / "v1" / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")


def make_m2(tables: Path) -> None:
    d = pa.dictionary(pa.int32(), pa.string())
    ids = IN_RANGE + [OUT_OF_RANGE]
    els = pa.table({"element_id": [f"{p.replace('.', '-')}#root1" for p in ids] + [f"{p.replace('.', '-')}#s1" for p in ids],
                    "arxiv_id": ids + ids,
                    "kind": pa.array(["document"] * 3 + ["sentence"] * 3).dictionary_encode().cast(d),
                    "content": ["root"] * 3 + ["We cite \\cite{vaswani}."] * 3})
    hier = pa.table({"arxiv_id": ids, "version": pa.array([1, 1, 1], pa.int32()),
                     "element_id": [f"{p.replace('.', '-')}#s1" for p in ids],
                     "parent_id": [f"{p.replace('.', '-')}#root1" for p in ids]})
    bib = pa.table({"arxiv_id": ids + ["2605.16616"], "citation_key": ["vaswani", "vaswani", "vaswani", "aitchison1975statistical"],
                    "entry_type": pa.array(["inproceedings"] * 4).dictionary_encode().cast(d),
                    "title": ["Attention is all you need"] * 4, "authors": [["Vaswani, Ashish"]] * 4,
                    "year": pa.array([2017] * 4, pa.int32()), "doi": [None] * 4, "journal": [None] * 4,
                    "url": ["https://arxiv.org/abs/1706.03762"] * 4})
    for name, t in (("elements", els), ("hierarchy", hier), ("bibtex", bib)):
        (tables / name).mkdir(parents=True)
        pq.write_table(t, tables / name / "part-0.parquet")


def make_predictions(path: Path, broken: bool = False) -> None:
    t = pa.table({"arxiv_id": IN_RANGE + [OUT_OF_RANGE],
                  "partition": pa.array(["train", "test", "valid"]).dictionary_encode(),
                  "citation_key": ["vaswani"] * 3,
                  "groundtruth_arxiv_id": ["1706.03762", None, "1706.03762"],
                  "cand_1": ["1706.03762", "9999.99999" if broken else "1810.04805", "1706.03762"],
                  "cand_2": ["1810.04805", "1706.03762", "1810.04805"],
                  "cand_3": [None, None, None], "cand_4": [None, None, None], "cand_5": [None, None, None],
                  "range": ["Bao", "Bao", "Quan"]})
    pq.write_table(t, path)


def run(module, argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = module.main(argv)
    return code, buf.getvalue()


def read(out: Path, table: str) -> pa.Table:
    return pa.concat_tables([pq.read_table(f) for f in sorted((out / "data" / table).glob("*.parquet"))])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.m1 = self.tmp / "m1"
        make_m1(self.m1)
        self.m2 = self.tmp / "m2"
        make_m2(self.m2)
        self.pred = self.tmp / "predictions.parquet"
        make_predictions(self.pred)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def export(self, *extra) -> Path:
        out = self.tmp / "hf"
        code, text = run(export_hf, ["--out", str(out), "--m1-data-dir", str(self.m1), "--m2-tables", str(self.m2),
                                     "--predictions", str(self.pred), *extra])
        self.assertEqual(code, 0, text)
        return out

    def test_all_tables_and_schemas(self):
        out = self.export()
        for name, schema in export_hf.SCHEMAS.items():
            files = sorted((out / "data" / name).glob("*.parquet"))
            self.assertTrue(files, name)
            self.assertRegex(files[0].name, rf"^{name}-00000-of-\d{{5}}\.parquet$")
            self.assertEqual(pq.read_schema(files[0]).names, schema.names, name)
            self.assertEqual([f.type for f in pq.read_schema(files[0])], [f.type for f in schema], name)
        self.assertEqual(read(out, "references").num_rows, 6)  # the reference without an arXiv ID is left out
        self.assertNotIn("range", read(out, "predictions").column_names)
        card = (out / "README.md").read_text(encoding="utf-8")
        for name in export_hf.TABLES:
            self.assertIn(f"- config_name: {name}", card)
            self.assertIn(f"path: data/{name}/*.parquet", card)
        self.assertTrue((self.tmp / "hf.manifest.json").is_file())  # next to the folder, never uploaded
        self.assertFalse((out / "hf.manifest.json").exists())

    def test_range_filter_every_table(self):
        out = self.export("--range", "2026-04", "05535", "2026-04", "05536")
        for name in export_hf.TABLES:
            ids = set(read(out, name).column("arxiv_id").to_pylist())
            self.assertTrue(ids <= set(IN_RANGE), (name, ids))
        self.assertEqual(read(out, "papers").num_rows, 2)
        same = self.export("--start", "2604.05535", "--end", "2604.05536")
        self.assertEqual(read(same, "references").num_rows, 4)

    def test_known_errors(self):
        keys = lambda out: set(zip(*(read(out, "bibtex").column(c).to_pylist() for c in ("arxiv_id", "citation_key"))))
        self.assertIn(("2605.16616", "aitchison1975statistical"), keys(self.export()))
        self.assertNotIn(("2605.16616", "aitchison1975statistical"), keys(self.export("--drop-known-errors")))
        card = lambda out: (out / "README.md").read_text(encoding="utf-8")  # noqa: E731
        self.assertIn("aitchison1975statistical", card(self.export()))  # the wrong row is in the export: noted
        self.assertNotIn("aitchison1975statistical", card(self.export("--start", "2604.05535", "--end", "2604.05536")))

    def test_snapshots_same_as_data_dir(self):
        """Archives replayed in order: a later version replaces a file, a removed file disappears."""
        backup = self.tmp / "backup"
        with redirect_stdout(io.StringIO()):
            snapshot.save(self.m1, backup, "first")
            make_m1(self.m1, title_suffix=" (revised)")
            shutil.rmtree(self.m1 / "corpus" / OUT_OF_RANGE.replace(".", "-"))
            snapshot.save(self.m1, backup, "second")
        self.assertEqual(len(snapshot.archives(backup)), 2)
        a, b = self.tmp / "from_dir", self.tmp / "from_snap"
        run(export_hf, ["--out", str(a), "--m1-data-dir", str(self.m1)])
        run(export_hf, ["--out", str(b), "--m1-snapshots", str(backup)])
        for name in ("papers", "references"):
            self.assertEqual(read(a, name).to_pylist(), read(b, name).to_pylist(), name)
        titles = read(b, "papers").column("title").to_pylist()
        self.assertTrue(all(t.endswith("(revised)") for t in titles), titles)
        self.assertEqual(len(titles), 2)  # the removed paper is gone

    def test_validate_passes_then_catches_errors(self):
        out = self.export()
        code, text = run(validate_m3, ["--dir", str(out)])
        self.assertEqual(code, 0, text)
        make_predictions(self.pred, broken=True)  # a candidate that is no reference of its paper
        out = self.export()
        code, text = run(validate_m3, ["--dir", str(out)])
        self.assertEqual(code, 1)
        self.assertIn("FAIL  predictions.cand_1 is a reference of the same paper", text)

    def test_several_runs_into_one_folder(self):
        out = self.tmp / "hf"
        run(export_hf, ["--out", str(out), "--m1-data-dir", str(self.m1)])
        code, _ = run(export_hf, ["--out", str(out), "--m2-tables", str(self.m2), "--predictions", str(self.pred)])
        self.assertEqual(code, 0)
        card = (out / "README.md").read_text(encoding="utf-8")
        for name in export_hf.TABLES:  # the card lists the tables of both runs
            self.assertIn(f"- config_name: {name}", card)
        self.assertEqual(run(validate_m3, ["--dir", str(out)])[0], 0)
        err = io.StringIO()
        with redirect_stderr(err):  # another range into the same folder is refused
            code, _ = run(export_hf, ["--out", str(out), "--predictions", str(self.pred),
                                      "--start", "2604.05535", "--end", "2604.05536"])
        self.assertEqual(code, 2)
        self.assertIn("export into a new folder", err.getvalue())

    def test_shards(self):
        """A new file every shard_bytes; the names say how many files there are."""
        writer = export_hf.ShardWriter(self.tmp / "hf", "references", export_hf.SCHEMAS["references"], shard_bytes=1)
        rows = [{"arxiv_id": "2604.05535", "ref_arxiv_id": f"1706.{i:05d}", "ref_title": "t", "ref_authors": ["a"],
                 "ref_submission_date": None, "ref_s2_id": None} for i in range(3)]
        for row in rows:
            writer.write(pa.Table.from_pylist([row], schema=export_hf.SCHEMAS["references"]))
        files = writer.close()
        self.assertEqual([f.name for f in files], [f"references-0000{i}-of-00003.parquet" for i in range(3)])
        self.assertEqual(read(self.tmp / "hf", "references").num_rows, 3)

    def test_bad_arguments(self):
        quiet = lambda: (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()))  # noqa: E731
        for argv in (["--out", "x"],  # no input
                     ["--out", "x", "--predictions", "p", "--start", "2604.00010", "--end", "2604.00001"],
                     ["--out", "x", "--predictions", "p", "--start", "2604.00010"],
                     ["--out", "x", "--tables", "bibtex", "--predictions", "p"]):  # no M2 input for bibtex
            out, err = quiet()
            with out, err, self.assertRaises(SystemExit):
                export_hf.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
