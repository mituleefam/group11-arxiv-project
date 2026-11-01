# Milestone 2: Reference Extraction & BibTeX Standardization

Tài liệu cho phần việc của Member 3: trích xuất tham khảo từ `\bibitem` và
file `.bib`, chuẩn hóa thành BibTeX, khử trùng lặp xuyên phiên bản và đổi
tên `\cite{}`. Kết quả cuối cùng đi vào bảng `bibtex`.

## 1. Các file

Toàn bộ code của Member 3 nằm trong thư mục con `src/reference_parser/`:

```
src/
├── m2_contract.py, pipeline_m2.py, ...     (Member 1)
└── reference_parser/                       (Member 3)
    ├── __init__.py
    ├── ref_text.py
    ├── bibitem_parser.py
    ├── bibtex_converter.py
    ├── dedup_bibtex.py
    ├── citation_updater.py
    ├── reference_stats.py
    └── test_m2_member3.py
```

| File                    | Vai trò                                                                                                                                                                             | Hàm pipeline gọi         |
| :---------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------- |
| `ref_text.py`         | Helper dùng chung: đọc nhóm `{...}`/`[...]`, bỏ comment `%`, LaTeX → Unicode (`{\"a}` → ä, `\&` → &), chuẩn hóa DOI / arXiv id / URL, khóa so sánh tiêu đề | –                         |
| `bibitem_parser.py`   | Tìm tham khảo của **một version**: `\bibitem` trong `thebibliography`, amsrefs `\bib{}`, entry của `.bib`                                                         | `parse(text, bib_files)` |
| `bibtex_converter.py` | Entry → dòng bảng `bibtex` (title, authors, year, doi, journal, url, entry_type)                                                                                                | `convert(entries)`       |
| `dedup_bibtex.py`     | Gộp tham khảo trùng xuyên các version, union field, chọn canonical key, sinh map đổi tên                                                                                    | `dedup(by_version)`      |
| `citation_updater.py` | Đổi key trong mọi lệnh `\cite`-like theo map                                                                                                                                   | `update(text, rename)`   |
| `reference_stats.py`  | Chạy riêng phần tham khảo trên corpus, ghi log và số liệu cho báo cáo                                                                                                      | CLI                        |
| `test_m2_member3.py`  | 22 test offline (gồm 1 test chạy cả `pipeline_m2`)                                                                                                                              | –                         |

Các module chỉ dùng thư viện chuẩn của Python (không cần cài thêm gói nào).
Chúng theo đúng contract trong `src/m2_contract.py` (branch `quan-m2-member1`).
Để pipeline tìm được module trong thư mục con, có 2 chỉnh sửa nhỏ ở file của Member 1:

- `m2_contract.py`: thêm `PACKAGES = ("", "reference_parser")`; `load()` tìm mỗi module
  ở `src/` trước, rồi trong `src/reference_parser/`.
- `pipeline_m2.py` (`code_version`): tính hash cả các file trong `src/reference_parser/`,
  để sửa code Member 3 thì lần chạy sau parse lại.

## 2. Vị trí trong pipeline (`pipeline_m2.py`)

```
gatherer.gather (M1)                       mỗi version: text đã gộp \input, đã bỏ comment
   │
   ├─ bibitem_parser.parse(text, bib_files)   ← đọc trên text THÔ, trước khi bỏ thebibliography
   ├─ bibtex_converter.convert(entries)
   │        (lặp cho mọi version)
   ├─ dedup_bibtex.dedup(by_version)  →  rows (bảng bibtex) + rename (cite_rename_map.json)
   │
   ├─ cleaner → normalizer (M2) → citation_updater.update(text, rename)
   └─ hierarchy.build (M1) + Registry (M4)   câu chỉ khác citation key giờ trùng nhau → 1 element_id
```

Đầu ra mỗi bài nằm ở `corpus/<yymm-id>/parsed/bibtex.json` và
`parsed/cite_rename_map.json`; `pipeline_m2.py` gom thành bảng Parquet
`tables/bibtex/`.

## 3. Trích xuất (`bibitem_parser.py`)

- **`\bibitem[label]{key} text`**: đọc trong `thebibliography` (và các môi
  trường `references`, `biblist`, `bibdiv`); nếu có `\bibitem` nằm ngoài các
  môi trường này thì quét cả văn bản. Label có ngoặc lồng (`[{Vaswani et~al.(2017)}]`)
  vẫn đọc đúng. Dòng comment (`% \bibitem{x}`) bị bỏ, `\%` và `%` trong `\url{}` giữ nguyên.
- **Markup REVTeX/natbib** `\bibinfo{title}{...}`, `\bibfield{author}{...}`
  được đọc thẳng thành field.
- **amsrefs** `\bib{key}{type}{author={...}, title={...}}`.
- **`.bib`**: parser tự viết, hỗ trợ `{...}` lồng, `"..."`, số trần,
  `@string` + nối `#`, tên tháng, `@comment`/`@preamble`, kế thừa `crossref`.
  Entry hỏng bị bỏ qua (có log) và đọc tiếp entry sau.
- **Chỉ giữ entry được trích dẫn** (giống BibTeX): key nằm trong `\cite`,
  `\citep`, `\citet*`, `\parencite`, `\nocite`, ... `\nocite{*}` hoặc tài liệu
  không có `\cite` nào → giữ hết. Số entry bị bỏ ghi vào log (`bib_uncited_dropped`).
- **File `.bib` fallback**: khi tài liệu không khai báo nguồn nào, `gatherer`
  đưa các `.bib` trong thư mục (`g.bib_fallback`); `reference_stats.py` ghi
  log mỗi lần dùng fallback.
- Cùng một key có cả `\bibitem` và entry `.bib` → gộp thành một (field của
  `.bib` ưu tiên, phần thiếu lấy từ `\bibitem`). Key định nghĩa 2 lần trong
  một version → giữ định nghĩa đầu (như BibTeX).

## 4. Chuẩn hóa BibTeX (`bibtex_converter.py`)

| Cột             | Cách lấy                                                                                                                                                                                                                                                                                                                                                                            |
| :--------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `citation_key` | key gốc (không rỗng)                                                                                                                                                                                                                                                                                                                                                               |
| `entry_type`   | type của `.bib` (alias biblatex như `online`, `report`, `thesis` được quy về chuẩn); với `\bibitem` đoán theo từ khóa: Proceedings/Conference/NeurIPS... → `inproceedings`; Journal/Trans./Lett./`21(3)`... → `article`; thesis → `phdthesis`/`mastersthesis`; Technical report → `techreport`; nhà xuất bản → `book`; còn lại `misc` |
| `title`        | bỏ ngoặc nhọn, accent → Unicode, bỏ dấu câu bao quanh                                                                                                                                                                                                                                                                                                                          |
| `authors`      | list `"Họ, Tên"`: tách `and` ở mức ngoài ngoặc; hỗ trợ `Last, First`, `First von Last`, `{Tên tổ chức}`, bỏ `others`/`et al.`                                                                                                                                                                                                                              |
| `year`         | int 1800 → năm sau; `.bib`: `year` hoặc `date`                                                                                                                                                                                                                                                                                                                               |
| `doi`          | regex `10.\d{4,9}/...`, bỏ tiền tố `https://doi.org/`, `doi:`, viết thường                                                                                                                                                                                                                                                                                                |
| `journal`      | `journal` / `journaltitle` / `booktitle` (bài trong kỷ yếu) / `series`; preprint arXiv → `arXiv preprint arXiv:<id>`                                                                                                                                                                                                                                                    |
| `url`          | `url`; nếu không có: `https://arxiv.org/abs/<id>` từ `eprint`, rồi `https://doi.org/<doi>`                                                                                                                                                                                                                                                                               |

**`\bibitem` là văn bản tự do**, đọc theo thứ tự:

1. DOI, URL, arXiv id bằng regex (`\doi{}`, `\url{}`, `\href{}`, link trần).
2. Năm: label natbib `(2017)` → `(2017)` sau tên tác giả (APA) → năm cuối cùng ngoài link/định danh.
3. Tách khối tác giả / tiêu đề / nơi đăng:
   - có `\newblock` (file `.bbl` do BibTeX sinh) → mỗi khối một phần;
   - tiêu đề trong ngoặc kép ...'' `` (IEEE);
   - `Tác giả (2017). Tiêu đề. Nơi đăng` (APA);
   - `Last, F., Last, F. Tiêu đề.` / `F. Last, F. Last. Tiêu đề.` / Vancouver `Smith J, Doe AB. Tiêu đề.`:
     cắt ở dấu chấm kết thúc câu, **không** cắt sau chữ viết tắt tên (`A.`, `J.-P.`) hay
     viết tắt thường gặp (`et al.`, `Proc.`, `Vol.`, `Phys.`...);
   - kiểu vật lý không có tiêu đề `A. Einstein, Ann. Phys. 17, 891 (1905)` → chỉ có tác giả + journal.
4. Phần in nghiêng (`\emph`, `{\em ...}`) trong nơi đăng được lấy làm `journal`.

Một tham khảo không đọc được không làm hỏng bài: nó thành dòng `misc` chỉ có key (có log).

## 5. Khử trùng lặp (`dedup_bibtex.py`)

Hai dòng (cùng version hoặc khác version) được coi là **một tham khảo** khi:

1. **Cùng citation key**, trừ khi mâu thuẫn rõ (DOI khác nhau, arXiv id khác nhau, hoặc
   tiêu đề khác hẳn). Ví dụ `\bibitem{1}` ở v1 và v2 trỏ tới hai bài khác nhau → **không** gộp.
2. **Cùng DOI**, **cùng arXiv id**, hoặc **cùng URL** (trừ khi tiêu đề khác hẳn).
3. **Cùng tiêu đề** sau chuẩn hóa (viết thường, bỏ dấu và ký tự đặc biệt; ≥ 3 từ
   hoặc ≥ 15 ký tự), năm không lệch quá 1 và họ tác giả đầu không khác nhau.
4. **Tiêu đề gần giống** (difflib ≥ 0.92) và cùng năm hoặc cùng họ tác giả đầu. Chỉ so
   các dòng chung một từ hiếm trong tiêu đề, nên vẫn nhanh với hàng nghìn tham khảo.

Các luật gộp bắc cầu (union-find).

**Canonical key**: key xuất hiện ở nhiều version nhất; hòa thì lấy key của
version mới nhất. Ví dụ của đề: `vaswani2017` (v1) + `vit_transformer` (v2, v3)
→ `vit_transformer`, map `{"vaswani2017": "vit_transformer"}`. Key là duy nhất
trong một bài (không phân biệt hoa thường); nếu key đã bị nhóm khác dùng thì
thêm hậu tố `_2`, `_3`.

**Union field**: mỗi field lấy giá trị tốt nhất trong nhóm, không bỏ thông tin:
ưu tiên nguồn có cấu trúc (`.bib`, amsrefs, `\bibinfo`) hơn đoán từ `\bibitem`,
rồi giá trị được nhiều dòng đồng ý nhất, rồi version mới nhất, rồi giá trị dài nhất.
`entry_type = misc` chỉ khi không dòng nào cho loại cụ thể hơn.
Ví dụ: v1 {title, author, year} + v2 {doi} + v3 {mất year} → {title, author, year, doi}.

**Giới hạn**: map đổi tên dùng chung cho mọi version (theo contract của
pipeline). Khi cùng một key trỏ tới hai bài khác nhau ở hai version, `\cite`
của key đó **không** bị đổi (để không trỏ sai), nhóm thứ hai nhận key có hậu
tố; số trường hợp này ghi ở cột `key_conflicts`.

## 6. Đổi tên `\cite` (`citation_updater.py`)

Đổi key trong `\cite`, `\citep`, `\citet*`, `\citealp`, `\citeauthor`,
`\citeyear`, `\nocite`, `\Cite...`, `\parencite`, `\textcite`, `\autocite`,
`\footcite`, multicite `\cites[..]{a}[..]{b}`; giữ nguyên lệnh, ghi chú
`[see][p.~3]` và thứ tự key. Key bị trùng sau khi đổi (`\cite{a,b}` mà a, b là
một bài) chỉ giữ một lần. Tra key chính xác trước, rồi không phân biệt hoa
thường nếu không mơ hồ. `\ref`, `\label` không bị đụng tới.

## 7. Log

Khi chạy trong `pipeline_m2.py`, module ghi qua logger `m2.references`
(cảnh báo như entry `.bib` hỏng in ra stderr). Để có log đầy đủ và số liệu
cho báo cáo, chạy:

```bash
python src/reference_parser/reference_stats.py --data-dir data --test       # 5 bài đầu
python src/reference_parser/reference_stats.py --data-dir data              # toàn bộ
python src/reference_parser/reference_stats.py --data-dir data --ids 2604.05535 --show   # xem từng dòng BibTeX và map
```

Ghi vào `data/logs/`:

| File                       | Nội dung                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| :------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reference_log.csv`      | 1 dòng / bài / lần chạy: số `\bibitem`, entry `.bib` (tổng / dùng / bỏ vì không được cite), version dùng `.bib` fallback, key được cite nhưng không tìm thấy, **số dòng trước và sau dedup**, số key đổi tên, `key_conflicts`, số lần gộp theo từng luật (key / doi / arxiv / url / title / fuzzy title), tỉ lệ có title/authors/year/doi/journal/url, số `misc`, thời gian, lỗi |
| `reference_versions.csv` | 1 dòng / version: trạng thái gather, file `.bib` dùng, có fallback không, có `thebibliography` không, các số đếm                                                                                                                                                                                                                                                                                                          |
| `reference_m2.log`       | cảnh báo và ghi chú của module, có mã bài `[2604.05535]`                                                                                                                                                                                                                                                                                                                                                                        |

Cuối lần chạy in tóm tắt: tổng entry trước → sau dedup (% giảm), số key đổi
tên, tỉ lệ field tìm được. Member 4 / Member 5 dùng `reference_log.csv` cho
phần thống kê "entry counts before and after deduplication".

## 8. Kiểm thử

```bash
python src/reference_parser/test_m2_member3.py      # 22 test, offline, không cần pyarrow
python src/test_m2_member1.py      # test của Member 1 vẫn chạy được khi có module Member 3
```

Có test cho: LaTeX → text, DOI/arXiv, `.bib` (@string, #, crossref, comment,
lọc entry không được cite, `\nocite{*}`), `\bibitem` các kiểu (natbib `.bbl`,
IEEE, APA, Vancouver, vật lý, sách), `\bibinfo`, amsrefs, ví dụ 3 version của đề,
gộp theo DOI/arXiv, key tái sử dụng cho bài khác, tiêu đề gần giống, ưu tiên
nguồn có cấu trúc, đổi tên `\cite` mọi biến thể, và một bài 2 version chạy qua
`pipeline_m2.py` (câu ở v1 và v2 chỉ khác key → thành 1 element).

Hiệu năng (đo trong sandbox): 1 version với 3000 entry `.bib` + 300 `\bibitem`,
dedup 4 version (5200 dòng) mất khoảng 0.7 giây.
