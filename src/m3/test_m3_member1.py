"""Tests of Milestone 3, Member 1 (no network, a few seconds).

    python src/m3/test_m3_member1.py
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

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates  # noqa: E402
import m3_data  # noqa: E402
import snapshot_diff  # noqa: E402
import split  # noqa: E402
import text_clean as tc  # noqa: E402


class TitleTests(unittest.TestCase):
    def test_same_form_on_both_sides(self):
        # bibtex (Milestone 2) vs Semantic Scholar, cases seen in the GBao range
        self.assertEqual(tc.title_clean("Attention is all you need"), tc.title_clean("Attention is All you Need"))
        self.assertEqual(tc.title_clean("BERT: Pre-training of deep bidirectional transformers"),
                         "bert pre training of deep bidirectional transformers")

    def test_latex(self):
        self.assertEqual(tc.title_clean(r"Bergman kernels over polarized K\"ahler manifolds"),
                         "bergman kernels over polarized kahler manifolds")
        self.assertEqual(tc.title_clean(r"K\"{a}hler"), "kahler")
        self.assertEqual(tc.title_clean("Kähler"), "kahler")
        self.assertEqual(tc.title_clean(r"Dyonic hairy black holes in $U(1)$ gauge theories"),
                         "dyonic hairy black holes in u 1 gauge theories")
        self.assertEqual(tc.title_clean(r"The $L^1$-regime of \emph{multiplicative} chaos"),
                         "the l 1 regime of multiplicative chaos")
        self.assertEqual(tc.title_clean(r"{DeepSeek}-{R1}: incentivizing reasoning"), "deepseek r1 incentivizing reasoning")
        self.assertEqual(tc.title_clean(r"Gr\"obner bases \& \c{C}ech cohomology"), "grobner bases cech cohomology")

    def test_greek(self):
        self.assertEqual(tc.title_clean(r"$\alpha$-synuclein"), tc.title_clean("α-synuclein"))
        self.assertEqual(tc.title_clean(r"$\Lambda$CDM"), "lambda cdm")
        self.assertEqual(tc.title_clean(r"$\mu$-law and \multicolumn"), "mu law and")

    def test_missing(self):
        self.assertIsNone(tc.title_clean(None))
        self.assertIsNone(tc.title_clean("  {} $ $ "))
        self.assertEqual(tc.title_tokens(None), [])

    def test_tokens(self):
        self.assertEqual(tc.title_tokens("Attention is all you need"), ["attention", "all", "need"])
        self.assertEqual(tc.title_tokens("Attention is all you need", remove_stop_words=False),
                         ["attention", "is", "all", "you", "need"])
        self.assertEqual(tc.title_tokens("To be or not to be"), ["to", "be", "or", "not", "to", "be"])


class AuthorTests(unittest.TestCase):
    def test_both_orders(self):
        self.assertEqual(tc.last_name("Vaswani, Ashish"), "vaswani")
        self.assertEqual(tc.last_name("Ashish Vaswani"), "vaswani")
        self.assertEqual(tc.last_name("J. Devlin"), "devlin")
        self.assertEqual(tc.last_name("Devlin, Jacob"), "devlin")

    def test_particles_suffixes_hyphens(self):
        self.assertEqual(tc.last_name("van Beethoven, Ludwig"), tc.last_name("Ludwig van Beethoven"))
        self.assertEqual(tc.last_name("King, Jr., Martin Luther"), "king")
        self.assertEqual(tc.last_name("Martin Luther King Jr."), "king")
        self.assertEqual(tc.last_name("Garcia-Perez, J."), tc.last_name("J. Garcia-Perez"))
        self.assertEqual(tc.last_name("Rui-Bing Wang"), "wang")
        self.assertEqual(tc.last_name("O'Brien, Kevin"), tc.last_name("Kevin O’Brien"))
        self.assertEqual(tc.last_name("Vida Dujmovi'c"), tc.last_name(r"Dujmovi{\'c}, Vida"))  # Semantic Scholar's accent

    def test_accents_and_latex(self):
        self.assertEqual(tc.last_name(r"M{\"u}ller, Hans"), "muller")
        self.assertEqual(tc.last_name("Hans Müller"), "muller")
        self.assertEqual(tc.last_name(r"G{\'e}rard Medioni"), "medioni")
        self.assertEqual(tc.last_name("Yao, D.-X. \\"), "yao")  # backslash left by Milestone 2

    def test_not_people(self):
        self.assertIsNone(tc.last_name("others"))
        self.assertIsNone(tc.last_name("et al."))
        self.assertIsNone(tc.last_name(""))
        self.assertEqual(tc.last_name("OpenAI"), "openai")
        self.assertEqual(tc.last_name("{LIGO Scientific Collaboration}"), "ligo scientific collaboration")

    def test_list(self):
        self.assertEqual(tc.authors_last(["Wei, Hua", "Zheng, Guanjie", "others"]), ["wei", "zheng"])
        self.assertEqual(tc.authors_last(["E. Özkan, H. Aydın, R. Dikici"]), ["ozkan", "aydin", "dikici"])
        self.assertEqual(tc.authors_last(["King, Jr., Martin Luther"]), ["king"])
        self.assertEqual(tc.authors_last(None), [])


class IdTests(unittest.TestCase):
    def test_arxiv(self):
        self.assertEqual(tc.find_arxiv_id("https://arxiv.org/abs/2406.05335"), "2406.05335")
        self.assertEqual(tc.find_arxiv_id("https://arxiv.org/pdf/2406.05335v3.pdf"), "2406.05335")
        self.assertEqual(tc.find_arxiv_id("arXiv preprint arXiv:2304.03010"), "2304.03010")
        self.assertEqual(tc.find_arxiv_id("10.48550/arXiv.2304.03010"), "2304.03010")
        self.assertEqual(tc.find_arxiv_id("CoRR abs/1810.04805"), "1810.04805")
        self.assertEqual(tc.find_arxiv_id("arXiv:0704.0001"), "0704.0001")
        self.assertEqual(tc.find_arxiv_id("hep-th/9901001"), "hep-th/9901001")
        self.assertEqual(tc.find_arxiv_id("arXiv:math.AG/0101001v2"), "math/0101001")
        self.assertEqual(tc.find_arxiv_id("cond-mat/0407066"), "cond-mat/0407066")

    def test_not_arxiv(self):
        self.assertIsNone(tc.find_arxiv_id("Phys. Rev. D 1234.5678"))       # no arXiv context
        self.assertIsNone(tc.find_arxiv_id("arXiv:0612.0001"))              # before April 2007
        self.assertIsNone(tc.find_arxiv_id("arXiv:1412.00001"))             # 5 digits before 2015
        self.assertIsNone(tc.find_arxiv_id("arXiv:2513.00001"))             # month 13
        self.assertIsNone(tc.find_arxiv_id("https://github.com/docs/1234567"))
        self.assertIsNone(tc.find_arxiv_id(None))

    def test_normalize(self):
        self.assertEqual(tc.normalize_arxiv_id("2511-23122"), "2511.23122")
        self.assertEqual(tc.normalize_arxiv_id("2406.05335v2"), "2406.05335")
        self.assertEqual(tc.normalize_arxiv_id("math.AG/0101001"), "math/0101001")
        self.assertEqual(tc.normalize_arxiv_id("hep-th/9901001"), "hep-th/9901001")

    def test_doi(self):
        self.assertEqual(tc.normalize_doi("https://doi.org/10.1073/PNAS.1117723109"), "10.1073/pnas.1117723109")
        self.assertEqual(tc.normalize_doi("doi:10.1145/3219819.3220096."), "10.1145/3219819.3220096")
        self.assertIsNone(tc.normalize_doi("not a doi"))
        self.assertEqual(tc.doi_from_url("https://doi.org/10.1086/175515"), "10.1086/175515")
        self.assertIsNone(tc.doi_from_url("https://github.com/x"))

    def test_year(self):
        self.assertEqual(tc.year_of("2025-11-28T12:11:17Z"), 2025)
        self.assertEqual(tc.year_of(2017), 2017)
        self.assertIsNone(tc.year_of(None))
        self.assertIsNone(tc.year_of("n.d."))


def write_fake_data(root: Path, bib_rows: list[dict], refs: dict[str, list[dict]]) -> tuple[Path, Path]:
    tables = root / "tables" / "bibtex"
    tables.mkdir(parents=True)
    schema = pa.schema([("arxiv_id", pa.string()), ("citation_key", pa.string()), ("entry_type", pa.string()),
                        ("title", pa.string()), ("authors", pa.list_(pa.string())), ("year", pa.int32()),
                        ("doi", pa.string()), ("journal", pa.string()), ("url", pa.string())])
    pq.write_table(pa.Table.from_pylist(bib_rows, schema=schema), tables / "part-00000.parquet")
    data = root / "m1"
    for pid, rs in refs.items():
        folder = data / "corpus" / pid.replace(".", "-")
        folder.mkdir(parents=True)
        folder.joinpath("references_all.json").write_text(json.dumps(
            {"arxiv_id": pid, "status": "success" if rs else "empty", "references": rs}), encoding="utf-8")
        folder.joinpath("references.json").write_text(json.dumps(
            {r["ref_arxiv_id"].replace(".", "-"): {"title": r["ref_title"], "authors": r["ref_authors"],
                                                   "submission_date": r["ref_submission_date"],
                                                   "semantic_scholar_id": r["ref_s2_id"]}
             for r in rs if r.get("ref_arxiv_id")}), encoding="utf-8")
    return root / "tables", data


def bib(pid, key, title, authors, year=None, doi=None, journal=None, url=None, entry_type="article"):
    return {"arxiv_id": pid, "citation_key": key, "entry_type": entry_type, "title": title, "authors": authors,
            "year": year, "doi": doi, "journal": journal, "url": url}


def ref(arxiv, title, authors, date, doi=None, year=None):
    ext = {"ArXiv": arxiv}
    if doi:
        ext["DOI"] = doi
    return {"ref_arxiv_id": arxiv, "ref_title": title, "ref_authors": authors, "ref_submission_date": date,
            "ref_s2_id": "s2" + (arxiv or "none"), "ref_year": year, "ref_venue": "arXiv.org", "ref_external_ids": ext}


# The paper 2604.05536 of the GBao range, shortened, plus one paper without references.
BIB = [
    bib("2604.05536", "nakaishi2024critical", "Critical phase transition in large language models",
        ["Nakaishi, Kai", "Nishikawa, Yoshihiko"], 2024, url="https://arxiv.org/abs/2406.05335"),
    bib("2604.05536", "altmann2012origin", "On the origin of long-range correlations in texts",
        ["Altmann, Eduardo G.", "Cristadoro, Giampaolo"], 2012, doi="10.1073/pnas.1117723109",
        url="https://doi.org/10.1073/pnas.1117723109"),
    bib("2604.05536", "vaswani2017attention", "Attention is all you need", ["Vaswani, Ashish", "others"], 2017),
    bib("2604.05536", "gloveweb", "GloVe: Global Vectors for Word Representation", ["Pennington, Jeffrey"], 2024,
        url="https://nlp.stanford.edu/projects/glove", entry_type="misc"),
    bib("2604.09999", "solo", None, [], None),
]
REFS = {
    "2604.05536": [
        ref("2406.05335", "Phase transition in large language models and the criticality of natural language",
            ["Kai Nakaishi", "Yoshihiko Nishikawa"], "2024-06-08T00:00:00Z", year=2024),
        ref("1207.0658", "On the origin of long-range correlations in texts", ["E. Altmann", "G. Cristadoro"],
            "2012-07-03T00:00:00Z", doi="10.1073/pnas.1117723109", year=2012),
        ref("1706.03762", "Attention is All you Need", ["Ashish Vaswani", "Noam Shazeer"], "2017-06-12T17:57:34Z",
            doi="10.48550/arXiv.1706.03762", year=2017),
        {"ref_arxiv_id": None, "ref_title": "A paper Semantic Scholar knows without an arXiv id",
         "ref_authors": ["X Y"], "ref_submission_date": None, "ref_s2_id": "s2x", "ref_year": 2001,
         "ref_venue": None, "ref_external_ids": {}},
    ],
    "2604.09999": [],
}


class DataTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.tables, self.m1 = write_fake_data(self.root, BIB, REFS)
        self.work = self.root / "work"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_data_candidates(self):
        self.assertEqual(m3_data.main(["--tables", str(self.tables), "--data-dir", str(self.m1),
                                       "--out", str(self.work)]), 0)
        entries, refs = m3_data.load(self.work)
        e = {r["citation_key"]: r for r in entries.to_pylist()}
        self.assertEqual(e["nakaishi2024critical"]["arxiv_id_in_entry"], "2406.05335")
        self.assertEqual(e["nakaishi2024critical"]["arxiv_id_from"], "url")
        self.assertEqual(e["altmann2012origin"]["doi"], "10.1073/pnas.1117723109")
        self.assertEqual(e["vaswani2017attention"]["authors_last"], ["vaswani"])
        self.assertEqual(e["vaswani2017attention"]["first_author_last"], "vaswani")
        self.assertIsNone(e["solo"]["title_clean"])
        r = {x["ref_arxiv_id"]: x for x in refs.to_pylist()}
        self.assertEqual(set(r), {"2406.05335", "1207.0658", "1706.03762"})  # the one without arXiv id is left out
        self.assertEqual(r["1706.03762"]["title_clean"], e["vaswani2017attention"]["title_clean"])
        self.assertEqual(r["1706.03762"]["first_author_last"], "vaswani")
        self.assertEqual(r["1706.03762"]["year"], 2017)
        self.assertEqual(r["1207.0658"]["doi"], e["altmann2012origin"]["doi"])

        stats = candidates.build(self.work)
        c = pq.read_table(self.work / "candidates.parquet").to_pylist()
        self.assertEqual(len(c), 4 * 3)  # 4 entries x 3 references; the paper without reference has no row
        self.assertEqual({(x["citation_key"], x["ref_arxiv_id"]) for x in c if x["citation_key"] == "gloveweb"},
                         {("gloveweb", "2406.05335"), ("gloveweb", "1207.0658"), ("gloveweb", "1706.03762")})
        s = {x["arxiv_id"]: x for x in stats}
        self.assertEqual((s["2604.05536"]["m"], s["2604.05536"]["n"], s["2604.05536"]["pairs"]), (4, 3, 12))
        self.assertEqual(s["2604.05536"]["arxiv_id_hits"], 1)
        self.assertEqual(s["2604.05536"]["doi_hits"], 1)
        self.assertEqual((s["2604.09999"]["m"], s["2604.09999"]["n"]), (1, 0))

    def test_subset_source_gives_same_ids(self):
        m3_data.main(["--tables", str(self.tables), "--data-dir", str(self.m1), "--out", str(self.work),
                      "--source", "subset"])
        _, refs = m3_data.load(self.work)
        self.assertEqual({x["ref_arxiv_id"] for x in refs.to_pylist()}, {"2406.05335", "1207.0658", "1706.03762"})

    def test_snapshot_diff(self):
        m3_data.main(["--tables", str(self.tables), "--data-dir", str(self.m1), "--out", str(self.work)])
        new_bib = [dict(r) for r in BIB if r["citation_key"] != "gloveweb"]
        new_bib[2]["title"] = "Attention is all you need!"
        new_bib.append(bib("2604.05536", "brown2020gpt3", "Language Models are Few-Shot Learners", ["Brown, Tom B."]))
        refs = {k: [dict(x) for x in v if x.get("ref_arxiv_id") != "2406.05335"] for k, v in REFS.items()}
        tables2, m1b = write_fake_data(self.root / "v2", new_bib, refs)
        work2 = self.root / "work2"
        m3_data.main(["--tables", str(tables2), "--data-dir", str(m1b), "--out", str(work2)])
        labels = self.root / "labels.csv"
        with open(labels, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["arxiv_id", "citation_key", "label_ref_arxiv_id", "source"])
            w.writerows([["2604.05536", "nakaishi2024critical", "2406.05335", "manual"],
                         ["2604.05536", "gloveweb", "", "manual"],
                         ["2604.05536", "altmann2012origin", "1207.0658", "manual"]])
        out = self.root / "diff.csv"
        snapshot_diff.main(["--old", str(self.work), "--new", str(work2), "--out", str(out), "--labels", str(labels)])
        with open(out, encoding="utf-8") as f:
            rows = {(r["table"], r["key"]): r for r in csv.DictReader(f)}
        self.assertEqual(rows[("entry", "gloveweb")]["change"], "removed")
        self.assertEqual(rows[("entry", "brown2020gpt3")]["change"], "added")
        self.assertEqual(rows[("entry", "vaswani2017attention")]["fields"], "title")
        self.assertEqual(rows[("reference", "2406.05335")]["change"], "removed")
        self.assertNotIn(("entry", "altmann2012origin"), rows)
        with open(self.root / "diff_labels.csv", encoding="utf-8") as f:
            bad = {r["citation_key"]: r["why"] for r in csv.DictReader(f)}
        self.assertEqual(set(bad), {"nakaishi2024critical", "gloveweb"})

    def test_pipeline_skips_missing_steps(self):
        out = subprocess.run([sys.executable, str(HERE / "pipeline_m3.py"), "--tables", str(self.tables),
                              "--data-dir", str(self.m1), "--work", str(self.work)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("[split] skipped: no label file", out.stdout)
        self.assertTrue((self.work / "candidates.parquet").is_file())


class SplitTests(unittest.TestCase):
    def sources(self, manual, auto):
        s = {f"m{i:02d}": "manual" for i in range(manual)}
        s.update({f"a{i:02d}": "auto" for i in range(auto)})
        return s

    def test_counts_per_member(self):
        r = split.make_split(self.sources(25, 40), members=5, seed=11)
        self.assertEqual(r["counts"]["test"], {"manual": 5, "auto": 5})
        self.assertEqual(r["counts"]["valid"], {"manual": 5, "auto": 5})
        self.assertEqual(r["counts"]["train"], {"manual": 15, "auto": 30})
        every = r["train"] + r["valid"] + r["test"]
        self.assertEqual(len(every), len(set(every)), 65)
        self.assertEqual(split.make_split(self.sources(25, 40), 5, 11)["test"], r["test"])  # same seed, same split

    def test_too_few(self):
        with self.assertRaises(ValueError):
            split.make_split(self.sources(9, 40), members=5, seed=1)

    def test_no_reference_papers_stay_in_train(self):
        s = self.sources(12, 12)
        rankable = set(s) - {"m00", "m01"}
        r = split.make_split(s, members=5, seed=3, rankable=rankable)
        self.assertIn("m00", r["train"])
        self.assertIn("m01", r["train"])

    def test_manual_wins(self):
        self.assertEqual(split.paper_sources([{"arxiv_id": "x", "source": "auto"}, {"arxiv_id": "x", "source": "manual"}]),
                         {"x": "manual"})
        with self.assertRaises(ValueError):
            split.paper_sources([{"arxiv_id": "x", "source": "maybe"}])


if __name__ == "__main__":
    unittest.main()
