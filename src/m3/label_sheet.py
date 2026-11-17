"""Milestone 3, labelling step 2 - the sheet a person fills in, and reading it back.

    python src/m3/label_sheet.py export --work WORK --picks WORK/manual_pick.csv --labeler NAME
                                        [--out WORK/label_sheet_NAME.xlsx]
    python src/m3/label_sheet.py import --work WORK --sheet WORK/label_sheet_NAME.xlsx --labeler NAME
                                        [--out WORK/labels_manual_NAME.csv] [--min-labelled 20]

export: one row per BibTeX entry of every picked publication (all entries),
sorted by publication then citation_key, with the entry as M2 wrote it, the
exact rule that matches it if any (rule_hint), and the 5 references of the
same publication that look the most like it (cand_1 .. cand_5, by score).
Nothing is filled in for the person: they write the reference in
label_ref_arxiv_id (empty = no reference of this publication is the entry)
and an x in `checked` once the row is decided, so that rows never looked at
can be told apart from "no match". The workbook also holds the sheet
"references" (every reference of the picked publications) and "README".
Without openpyxl, the same rows go to CSV files (UTF-8 with BOM for Excel).

Score of a candidate (only to order the suggestions; the person decides):
  0.6 * title similarity (difflib ratio of the sorted title words)
+ 0.3 * Jaccard of the author last names
+ 0.1 * year closeness (1 same year, 0.5 one year apart, else 0)
and 1.0 when an exact rule (same arXiv id, same DOI) names the reference.

import: reads the filled sheet back, checks it and writes the label file
    arxiv_id, citation_key, label_ref_arxiv_id, is_match, source, labeler, note
(source = "manual"; label_ref_arxiv_id empty = "no match"). This is the
format split.py (arxiv_id, source) and snapshot_diff.py (citation_key,
label_ref_arxiv_id) read. Only a matched pair is a label (lecturer's Q&A):
an entry decided as "no match" is kept in the file with an empty
label_ref_arxiv_id, so that the work is not lost and the predictions table can
show it, but it does not count towards --min-labelled and is not a sample of
MRR@5. Problems (row not checked, a reference that is not one of the
publication's, a publication with fewer than --min-labelled matched pairs, the
same entry twice) are listed and the exit code is 1.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

ENTRY_COLS = ["arxiv_id", "citation_key", "entry_type", "title", "authors", "year", "journal", "url", "doi",
              "arxiv_id_in_entry"]
N_CAND = 5
CAND_COLS = [f"cand_{k}_{c}" for k in range(1, N_CAND + 1) for c in ("ref_arxiv_id", "title", "authors", "year", "score")]
FILL_COLS = ["label_ref_arxiv_id", "checked", "note"]
SHEET_COLS = ENTRY_COLS + ["rule_hint"] + FILL_COLS + CAND_COLS
REF_COLS = ["arxiv_id", "ref_arxiv_id", "ref_title", "ref_authors", "year", "year_s2", "doi"]
LABEL_COLS = ["arxiv_id", "citation_key", "label_ref_arxiv_id", "is_match", "source", "labeler", "note"]
MIN_PER_LABELER = 100  # matched pairs per member (5 publications x 20)

README = """Gán nhãn tay - cách điền
Mỗi dòng của sheet "entries" là một BibTeX entry của một bài đã chọn (mọi entry của bài).
1. Đọc title / authors / year / journal / url / doi của entry.
2. So với các reference CỦA CÙNG BÀI: xem 5 gợi ý cand_1..cand_5 (sắp theo điểm giống), hoặc tra sheet
   "references" (lọc theo arxiv_id) nếu không gợi ý nào đúng.
3. Điền label_ref_arxiv_id = ref_arxiv_id của reference đúng (chép y nguyên, ví dụ 1706.03762).
   Không có reference nào của bài là entry này: để TRỐNG label_ref_arxiv_id.
4. Điền x vào cột checked khi đã quyết định xong dòng đó (kể cả khi để trống = không có).
   Dòng chưa có x bị coi là chưa xem, lệnh import sẽ báo.
   Theo giải đáp của thầy: chỉ cặp (entry, reference) khớp mới là NHÃN. Mỗi bài cần >= 20 cặp khớp;
   entry "không có" không tính vào 20 và không tính vào MRR@5 (nhưng vẫn đánh x để biết đã xem).
5. Cột note: ghi chú tuỳ ý (ví dụ "title viết tắt", "bản tạp chí của bài arXiv").
rule_hint chỉ là gợi ý của luật khớp chính xác (cùng arXiv ID / cùng DOI / title giống hệt); vẫn phải tự kiểm tra.
Một entry chỉ khớp tối đa một reference. Không sửa các cột khác, không xoá dòng.
Xong thì chạy: python src/m3/label_sheet.py import --work WORK --sheet <file này> --labeler <tên>
"""


def natural_key(s: str) -> list:
    """citation keys 1, 2, 10 (not 1, 10, 2); text keys in their usual order."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower()) for t in re.split(r"(\d+)", s) if t]


def _title_words(s: str | None) -> str:
    return " ".join(sorted((s or "").split()))


def score(entry: dict, ref: dict) -> float:
    t = difflib.SequenceMatcher(None, _title_words(entry["title_clean"]), _title_words(ref["title_clean"])).ratio() \
        if entry["title_clean"] and ref["title_clean"] else 0.0
    a, b = set(entry["authors_last"] or []), set(ref["authors_last"] or [])
    au = len(a & b) / len(a | b) if a and b else 0.0
    years = [y for y in (ref["year"], ref["year_s2"]) if y]
    gap = min((abs(entry["year"] - y) for y in years), default=None) if entry["year"] else None
    yr = 1.0 if gap == 0 else 0.5 if gap == 1 else 0.0
    return 0.6 * t + 0.3 * au + 0.1 * yr


def exact_rule(entry: dict, refs: list[dict]) -> tuple[str | None, str | None]:
    """(rule, ref_arxiv_id) of the first exact rule that names one reference, else (None, None)."""
    ids = {r["ref_arxiv_id"] for r in refs}
    if entry["arxiv_id_in_entry"] in ids:
        return "same arXiv id", entry["arxiv_id_in_entry"]
    if entry["doi"]:
        for r in refs:
            if entry["doi"] in (r["doi"], f"10.48550/arxiv.{r['ref_arxiv_id']}"):
                return "same DOI", r["ref_arxiv_id"]
    if entry["title_clean"]:
        same = [r["ref_arxiv_id"] for r in refs if r["title_clean"] == entry["title_clean"]]
        if len(same) == 1:
            return "same title", same[0]
    return None, None


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    return str(v)


def build_rows(work: Path, picks: list[str]) -> tuple[list[dict], list[dict]]:
    wanted = set(picks)
    entries = [e for e in pq.read_table(work / "entries_clean.parquet").to_pylist() if e["arxiv_id"] in wanted]
    refs = defaultdict(list)
    for r in pq.read_table(work / "references_clean.parquet").to_pylist():
        if r["arxiv_id"] in wanted:
            refs[r["arxiv_id"]].append(r)
    order = {p: i for i, p in enumerate(picks)}
    entries.sort(key=lambda e: (order[e["arxiv_id"]], natural_key(e["citation_key"])))
    rows = []
    for e in entries:
        paper_refs = refs.get(e["arxiv_id"], [])
        rule, rule_ref = exact_rule(e, paper_refs)
        ranked = sorted(paper_refs, key=lambda r: (-(1.0 if r["ref_arxiv_id"] == rule_ref and rule != "same title"
                                                       else score(e, r)), r["ref_arxiv_id"]))
        row = {c: _text(e.get(c)) for c in ENTRY_COLS}
        row["rule_hint"] = f"{rule} -> {rule_ref}" if rule else ""
        row.update({c: "" for c in FILL_COLS})
        for k in range(N_CAND):
            r = ranked[k] if k < len(ranked) else None
            s = (1.0 if r and r["ref_arxiv_id"] == rule_ref and rule != "same title" else score(e, r)) if r else None
            row[f"cand_{k + 1}_ref_arxiv_id"] = r["ref_arxiv_id"] if r else ""
            row[f"cand_{k + 1}_title"] = r["ref_title"] or "" if r else ""
            row[f"cand_{k + 1}_authors"] = _text(r["ref_authors"]) if r else ""
            row[f"cand_{k + 1}_year"] = _text(r["year"]) if r else ""
            row[f"cand_{k + 1}_score"] = round(s, 3) if s is not None else ""
        rows.append(row)
    ref_rows = [{c: _text(r.get(c)) for c in REF_COLS} for p in picks for r in
                sorted(refs.get(p, []), key=lambda r: r["ref_arxiv_id"])]
    return rows, ref_rows


def write_workbook(path: Path, rows: list[dict], ref_rows: list[dict]) -> Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        base = path.with_suffix("")
        for name, cols, data in (("", SHEET_COLS, rows), ("_references", REF_COLS, ref_rows)):
            with open(f"{base}{name}.csv", "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(data)
        Path(f"{base}_README.txt").write_text(README, encoding="utf-8")
        print("openpyxl is not installed: wrote CSV files instead of a workbook")
        return Path(f"{base}.csv")
    wb = Workbook()
    ws = wb.active
    ws.title = "entries"
    ws.append(SHEET_COLS)
    for r in rows:
        ws.append([r[c] for c in SHEET_COLS])
    fill = PatternFill("solid", fgColor="FFF2CC")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        if cell.value in FILL_COLS:
            cell.fill = fill
    for col in FILL_COLS:
        letter = ws.cell(row=1, column=SHEET_COLS.index(col) + 1).column_letter
        for cell in ws[letter][1:]:
            cell.fill = fill
            cell.number_format = "@"  # text: Excel must not turn 1706.03762 into a number
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    widths = {"title": 50, "authors": 30, "journal": 25, "url": 25, "rule_hint": 28, "note": 25}
    for i, c in enumerate(SHEET_COLS, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = \
            widths.get(c, 40 if c.endswith("_title") else 14)
    wr = wb.create_sheet("references")
    wr.append(REF_COLS)
    for r in ref_rows:
        wr.append([r[c] for c in REF_COLS])
    wr.freeze_panes = "C2"
    wr.auto_filter.ref = wr.dimensions
    wr.column_dimensions["C"].width = 60
    wr.column_dimensions["D"].width = 40
    wm = wb.create_sheet("README")
    for line in README.splitlines():
        wm.append([line])
    wm.column_dimensions["A"].width = 120
    wb.save(path)
    return path


def read_sheet(path: Path) -> list[dict]:
    if path.suffix == ".xlsx":
        from openpyxl import load_workbook
        ws = load_workbook(path, read_only=True, data_only=True)["entries"]
        it = ws.iter_rows(values_only=True)
        header = [str(h) if h is not None else "" for h in next(it)]
        return [{h: ("" if v is None else str(v)).strip() for h, v in zip(header, row)} for row in it
                if any(v not in (None, "") for v in row)]
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def check(rows: list[dict], refs: dict[str, set[str]], min_labelled: int) -> tuple[list[dict], list[str]]:
    problems, labels, seen = [], [], Counter()
    for i, r in enumerate(rows, 2):  # row 1 is the header
        pid, key = r.get("arxiv_id", ""), r.get("citation_key", "")
        if not pid or not key:
            problems.append(f"row {i}: no arxiv_id / citation_key")
            continue
        seen[(pid, key)] += 1
        if r.get("checked", "").lower() not in ("x", "1", "y", "yes", "true"):
            problems.append(f"row {i}: {pid} {key} not checked")
            continue
        label = r.get("label_ref_arxiv_id", "")
        if label.lower() in ("none", "-"):
            label = ""
        if label and label not in refs.get(pid, set()):
            problems.append(f"row {i}: {pid} {key}: {label} is not a reference of {pid}")
            continue
        labels.append({"arxiv_id": pid, "citation_key": key, "label_ref_arxiv_id": label, "note": r.get("note", "")})
    problems += [f"{pid} {key}: {n} rows" for (pid, key), n in seen.items() if n > 1]
    per_paper = Counter(x["arxiv_id"] for x in labels if x["label_ref_arxiv_id"])  # labels = matched pairs only
    problems += [f"{pid}: {per_paper[pid]} matched pairs, at least {min_labelled} needed"
                 for pid in sorted({r.get("arxiv_id") for r in rows if r.get("arxiv_id")})
                 if per_paper[pid] < min_labelled]
    return labels, problems


def cmd_export(args) -> int:
    with open(args.picks, encoding="utf-8", newline="") as f:
        picks = [r["arxiv_id"] for r in csv.DictReader(f)]
    if args.range:
        with open(args.picks, encoding="utf-8", newline="") as f:
            picks = [r["arxiv_id"] for r in csv.DictReader(f) if r.get("range") == args.range]
    if not picks:
        print("no publication to export", file=sys.stderr)
        return 1
    rows, ref_rows = build_rows(args.work, picks)
    out = write_workbook(args.out or args.work / f"label_sheet_{args.labeler}.xlsx", rows, ref_rows)
    hinted = sum(1 for r in rows if r["rule_hint"])
    print(f"{len(picks)} publications, {len(rows)} entries ({hinted} with a rule_hint), "
          f"{len(ref_rows)} references -> {out}")
    for p in picks:
        n = sum(1 for r in rows if r["arxiv_id"] == p)
        print(f"  {p}: {n} entries, {sum(1 for r in ref_rows if r['arxiv_id'] == p)} references")
    return 0


def cmd_import(args) -> int:
    rows = read_sheet(args.sheet)
    pids = {r.get("arxiv_id") for r in rows}
    refs = defaultdict(set)
    for r in pq.read_table(args.work / "references_clean.parquet", columns=["arxiv_id", "ref_arxiv_id"]).to_pylist():
        if r["arxiv_id"] in pids:
            refs[r["arxiv_id"]].add(r["ref_arxiv_id"])
    labels, problems = check(rows, refs, args.min_labelled)
    if problems:
        print(f"{len(problems)} problems, nothing written:")
        for p in problems[:200]:
            print("  " + p)
        return 1
    out = args.out or args.work / f"labels_manual_{args.labeler}.csv"
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_COLS)
        w.writeheader()
        for x in labels:
            w.writerow({**x, "is_match": "true" if x["label_ref_arxiv_id"] else "false",
                        "source": "manual", "labeler": args.labeler})
    matched = sum(1 for x in labels if x["label_ref_arxiv_id"])
    print(f"{len(labels)} entries decided in {len(set(x['arxiv_id'] for x in labels))} publications: "
          f"{matched} labels (matched pairs), {len(labels) - matched} no match (not labels, not in |Q|) -> {out}")
    if matched < MIN_PER_LABELER:
        print(f"!! {matched} matched pairs for {args.labeler}: a member needs at least {MIN_PER_LABELER}")
    twice = Counter((x["arxiv_id"], x["label_ref_arxiv_id"]) for x in labels if x["label_ref_arxiv_id"])
    for (pid, rid), n in sorted(twice.items()):
        if n > 1:
            print(f"!! {pid}: reference {rid} is the label of {n} entries (same work cited twice?), check them")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write the sheet to fill in")
    e.add_argument("--work", type=Path, required=True)
    e.add_argument("--picks", type=Path, required=True, help="manual_pick.csv of select_manual.py")
    e.add_argument("--labeler", required=True, help="name of the person labelling (goes in the file name)")
    e.add_argument("--range", help="only the picks of this range (column range of the picks file)")
    e.add_argument("--out", type=Path, help="default WORK/label_sheet_<labeler>.xlsx")
    i = sub.add_parser("import", help="check a filled sheet and write the label file")
    i.add_argument("--work", type=Path, required=True)
    i.add_argument("--sheet", type=Path, required=True, help="filled .xlsx (sheet entries) or .csv")
    i.add_argument("--labeler", required=True)
    i.add_argument("--min-labelled", type=int, default=20, help="matched pairs needed per publication")
    i.add_argument("--out", type=Path, help="default WORK/labels_manual_<labeler>.csv")
    args = p.parse_args(argv)
    return cmd_export(args) if args.cmd == "export" else cmd_import(args)


if __name__ == "__main__":
    sys.exit(main())
