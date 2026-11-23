# M3 – Member 1: nạp dữ liệu, làm sạch, cặp ứng viên, chia tập

Phần này biến hai bảng thô thành đầu vào chung cho cả nhóm:

- bảng `bibtex` của M2 (entry mỗi bài, tác giả tự gõ nên lộn xộn),
- references của M1 (Semantic Scholar, chỉ lấy reference có arXiv ID).

Ra hai bảng đã làm sạch, cùng một dạng ở cả hai phía, và bảng các cặp (entry, reference) trong từng bài. Member 2 gán nhãn, Member 3 tính đặc trưng, Member 4 train đều đọc mấy file này.

Code nằm trong `src/m3/`, chỉ cần `pyarrow` (đã có trong `requirements.txt`).

## Chạy

```bash
python src/m3/pipeline_m3.py --tables M2_TABLES --data-dir M1_DATA_DIR [M1_DATA_DIR ...] --work WORK
```

- `M2_TABLES`: thư mục `tables/` mà `pipeline_m2.py` ghi ra (có `bibtex/*.parquet`). Ví dụ `/content/m2_work/_merged/tables`.
- `M1_DATA_DIR`: thư mục dữ liệu M1 đã khôi phục (có `corpus/<yymm-id>/references_all.json`), ví dụ `/content/g11/GBao_2604-05535_2604-10534`. Nhiều khoảng thì liệt kê hết.
- `WORK`: thư mục làm việc của M3, mọi file bên dưới ghi vào đây.

Lệnh này chạy lần lượt `m3_data.py` → `candidates.py` → `split.py` (khi đã có file nhãn) → các bước sau của Member 3, 4. Bước nào chưa có script thì in "skipped" rồi đi tiếp. Chạy riêng từng bước cũng được:

```bash
python src/m3/m3_data.py --tables M2_TABLES --data-dir M1_DATA_DIR --out WORK
python src/m3/candidates.py --work WORK
python src/m3/split.py --labels WORK/labels.csv --members 5 --seed 11 --stats WORK/paper_stats.csv --out WORK/splits.json
python src/m3/snapshot_diff.py --old WORK_CU --new WORK_MOI --out diff.csv --labels WORK/labels.csv
python src/m3/test_m3_member1.py
```

Thử trên khoảng của Bảo (5000 bài) trên máy thường: nạp + làm sạch khoảng 45 s, sinh cặp 6 s.

## File ra (trong `WORK`)

| File | Một dòng là | Khoá |
|---|---|---|
| `entries_clean.parquet` | một entry BibTeX | `(arxiv_id, citation_key)` |
| `references_clean.parquet` | một reference có arXiv ID | `(arxiv_id, ref_arxiv_id)` |
| `candidates.parquet` | một cặp (entry, reference) cùng bài | `(arxiv_id, citation_key, ref_arxiv_id)` |
| `paper_stats.csv` | một bài: m, n, m×n và mấy con số giúp chọn bài gán nhãn | `arxiv_id` |
| `papers_refs.csv` | một bài: trạng thái Semantic Scholar, số reference, số reference có arXiv ID | `arxiv_id` |
| `splits.json` | `{"train": [...], "valid": [...], "test": [...]}` | |

Mọi ID bài và ID reference đều ở dạng chấm (`2604.05536`, `1706.03762`; ID kiểu cũ `hep-th/9901001`), giống bảng `bibtex`.

### Cột của `entries_clean.parquet`

| Cột | Là gì |
|---|---|
| `entry_type`, `title`, `authors`, `journal`, `url` | giữ nguyên từ bảng `bibtex`, để công cụ gán nhãn hiện ra cho người xem |
| `title_clean` | tiêu đề đã làm sạch (xem dưới); `None` khi không có tiêu đề |
| `title_tokens` | các từ của tiêu đề, đã bỏ stop-word |
| `authors_last` | họ của từng tác giả, theo thứ tự |
| `first_author_last` | họ tác giả đầu |
| `year` | năm (số nguyên) |
| `doi` | DOI đã chuẩn hoá; lấy từ cột `doi`, không có thì lấy từ link `doi.org` trong `url` |
| `arxiv_id_in_entry` | arXiv ID mà entry tự ghi, đã chuẩn hoá |
| `arxiv_id_from` | cột tìm thấy ID đó (`url`, `journal`, `doi`, `title`) |

### Cột của `references_clean.parquet`

| Cột | Là gì |
|---|---|
| `ref_title`, `ref_authors`, `ref_submission_date`, `ref_s2_id` | giữ nguyên, đúng các cột bảng `references` của đề M1 |
| `title_clean`, `title_tokens`, `authors_last`, `first_author_last` | làm sạch y như bên entry |
| `year` | năm nộp arXiv v1 (từ `ref_submission_date`) |
| `year_s2` | năm Semantic Scholar ghi (`ref_year`, thường là năm đăng tạp chí) |
| `doi` | DOI Semantic Scholar có (86% reference có); dạng `10.48550/arxiv.<id>` là DOI của chính arXiv |
| `venue` | nơi đăng, Semantic Scholar ghi |

## Ý tưởng và cách làm

### 1. Nạp dữ liệu (`m3_data.py`)

- Entry: đọc thẳng các file Parquet của bảng `bibtex`.
- Reference: đọc `references_all.json` của từng bài, **chỉ giữ dòng có `ref_arxiv_id`**. Đề ghi bảng `references` "keeping the arXiv-keyed references only"; reference không có arXiv ID thì không bao giờ là đáp án.
- Vì sao đọc `references_all.json` chứ không phải `references.json` như bản phân công: hai file có cùng các reference arXiv (mình so cả 5000 bài, khớp 100%), nhưng `references.json` không có năm, DOI, venue, và khoá viết dạng `2511-23122`. Luật DOI của Member 2 và đặc trưng năm của Member 3 cần các cột đó. Vẫn chọn được file kia bằng `--source subset`.
- **Không sửa dữ liệu**: cột gốc giữ nguyên, cột làm sạch thêm bên cạnh. Chỗ nào lạ thì in ra để báo, không vá (xem mục "Lỗi dữ liệu").

### 2. Làm sạch (`text_clean.py`)

Nguyên tắc: **hai phía đi qua đúng cùng một hàm**, vì chỉ cần lệch một chút cách chuẩn hoá là "Attention is all you need" với "Attention is All you Need" thành khác nhau.

**Tiêu đề** (`title_clean`):

1. Bỏ LaTeX:
   - dấu `\"a` → `a`, `\c{c}` → `c`;
   - chữ Hy Lạp `\alpha` → `alpha` (và `α` → `alpha`, để `$\alpha$-synuclein` khớp `α-synuclein`);
   - lệnh khác bỏ tên giữ nội dung: `\emph{Deep}` → `Deep`;
   - bỏ `$`, `{}`, `~`.
2. Bỏ dấu tiếng nước ngoài: `Kähler` → `Kahler`, `Łukasz` → `Lukasz`.
3. Chữ thường; dấu nháy bỏ hẳn (`O'Brien` → `obrien`); mọi dấu câu khác (gạch nối, hai chấm, ngoặc...) thành khoảng trắng, gộp khoảng trắng.

Ví dụ: `K\"ahler-Einstein metrics: $U(1)$` → `kahler einstein metrics u 1`.

Tiêu đề phía Semantic Scholar cũng còn LaTeX (`$` ở 525 tiêu đề, `\` ở 282), nên làm sạch cả hai phía là bắt buộc.

**Từ của tiêu đề** (`title_tokens`): tách `title_clean` theo khoảng trắng, bỏ stop-word. Danh sách stop-word cố tình ngắn (a, the, of, is, for, with...), không bỏ "deep", "model", "learning". Nếu bỏ xong không còn gì (tiêu đề toàn stop-word) thì giữ nguyên. Muốn giữ hết thì chạy với `--keep-stop-words`.

**Tên tác giả** (`authors_last`, `first_author_last`): chỉ lấy **họ**, vì tên riêng hai bên viết rất khác nhau:

- BibTeX gần như luôn là "Họ, Tên" (`Vaswani, Ashish`), Semantic Scholar là "Tên Họ" (`Ashish Vaswani`), 40% chỉ có tên viết tắt (`J. Devlin`).
- Có dấu phẩy thì họ là phần trước dấu phẩy, không có thì là từ cuối. Cả hai ra `vaswani`.
- Lấy **từ cuối của họ** để hai phía giống nhau:
  - `van Beethoven, Ludwig` và `Ludwig van Beethoven` đều ra `beethoven`;
  - `Garcia-Perez, J.` và `J. Garcia-Perez` đều ra `perez`.
- Bỏ đuôi `Jr.`, `III`. Bỏ `others`, `et al.` (không phải người).
- Tổ chức (tên có collaboration, consortium, team...) giữ cả tên: `ligo scientific collaboration`.
- Một chuỗi BibTeX chứa nhiều người (`E. Özkan, H. Aydın, R. Dikici`, M2 không tách) thì tách ra.

Giới hạn, cố tình không đoán:
- Tên châu Á bị đảo thứ tự mà không có dấu phẩy (`Wang Rui-Bing` so với `Rui-Bing Wang`) sẽ ra họ khác nhau.
- Tên chỉ toàn chữ viết tắt (`A. J`) ra `j`.

Member 3 nên dùng thêm độ trùng của cả tập họ, không chỉ tác giả đầu.

**Năm**:
- Bên entry là cột `year`.
- Bên reference có hai năm: `year` = năm nộp arXiv v1, `year_s2` = năm Semantic Scholar ghi. Hai năm này lệch nhau ở 1.139/92.916 reference (1,2%), thường do bản preprint và bản đăng tạp chí khác năm. So năm nên cho lệch ±1 và lấy độ lệch nhỏ hơn trong hai năm.

**arXiv ID trong entry** (`arxiv_id_in_entry`): tìm lần lượt trong `url` → `journal` → `doi` → `title`.

- ID mới (`2406.05335`) chỉ lấy khi chuỗi có chữ arXiv hoặc CoRR (link `arxiv.org`, `arXiv:`, DOI `10.48550/arXiv.`, `CoRR abs/...`). Có vậy thì số trang hay số báo cáo như `1234.5678` mới không bị lấy nhầm.
- Kiểm tra ID hợp lệ: tháng 01–12, từ 04/2007 trở đi, phần số có 4 chữ số trước 2015 và 5 chữ số từ 2015.
- ID cũ (`hep-th/9901001`) lấy ở đâu cũng được, vì tên archive đã đủ rõ.
- Chuẩn hoá: chữ thường, bỏ `v2`, bỏ phân lớp ở ID cũ (`math.AG/0101001` → `math/0101001`, dạng arXiv và Semantic Scholar dùng).

**DOI**: chữ thường, bỏ `https://doi.org/`, `doi:` và dấu câu ở cuối. Một bài có thể có hai DOI, một của tạp chí và một của arXiv (`10.48550/arxiv.<id>`). 21.988 reference phía Semantic Scholar chỉ có DOI arXiv, nên khi so DOI nhớ so cả `10.48550/arxiv.<ref_arxiv_id>`.

### 3. Cặp ứng viên (`candidates.py`)

- Mỗi bài có m entry và n reference thì ghi m×n dòng `(arxiv_id, citation_key, ref_arxiv_id)`, đúng ma trận ứng viên của đề. Ghép chỉ trong cùng một bài.
- Bài có n = 0 (không có reference arXiv nào) **không có dòng nào**: mọi entry của nó là "không có".
- Ghi theo từng khối 500.000 dòng, nên 6 triệu cặp không cần nằm hết trong RAM.
- `paper_stats.csv`, mỗi bài một dòng:
  - `m`, `n`, `pairs`;
  - `entries_with_arxiv_id`, `arxiv_id_hits`: số entry tự ghi arXiv ID, và trong đó bao nhiêu ID có trong reference của bài;
  - `entries_with_doi`, `doi_hits`: số entry có DOI, và bao nhiêu DOI trùng reference;
  - `entries_without_title`, `misc_entries`.

  Member 2 dùng file này để chọn bài gán tay: m ≥ 20 (đề yêu cầu ≥ 20 entry), n > 0, ít entry không tiêu đề.

### 4. Chia tập (`split.py`)

- Chia **theo bài**, không theo entry, để một bài không nằm ở cả train lẫn test.
- Đề: "a group of k members puts k manually labeled and k automatically matched publications into each of the two sets". Nhóm 5 người thì **test 5 bài gán tay + 5 bài tự động, valid cũng 5 + 5**, còn lại là train. Bản phân công ghi 1 + 1, mình làm theo đề (`--members 5`), các bạn xem lại giúp.
- Đọc file nhãn của Member 2 (CSV hoặc Parquet, cần cột `arxiv_id` và `source` = `manual`/`auto`). Bài có ít nhất một nhãn tay thì tính là bài gán tay.
- Bài n = 0 không được rút vào test/valid (không có ứng viên để xếp hạng), vẫn ở train. Cần truyền `--stats`.
- Cùng `--seed` thì ra cùng kết quả. Thiếu bài thì báo lỗi (cần ≥ 10 bài mỗi loại).

### 5. So hai bản dữ liệu (`snapshot_diff.py`)

Khi M1 hoặc M2 chạy lại, `citation_key` và reference có thể đổi, mà nhãn lại gắn với khoá. Cách dùng:

1. Chạy `m3_data.py` cho bản cũ và bản mới vào hai thư mục.
2. Chạy `snapshot_diff.py` trên hai thư mục đó. Nó liệt kê theo từng bài các entry và reference bị **mất**, **mới**, hoặc **đổi nội dung** (ghi rõ cột nào đổi).
3. Có `--labels` thì ghi thêm `diff_labels.csv`: các nhãn phải xem lại, vì entry đã đổi hoặc mất, hoặc reference được gán không còn nữa.

### 6. Pipeline (`pipeline_m3.py`)

Quy ước cho các bước sau (Member 3, 4): mỗi script chạy được bằng

```
python src/m3/<script>.py --work WORK
```

và đọc, ghi file trong `WORK` (`features.parquet`, model, `predictions`...). Pipeline gọi `features.py`, `model.py`, `predict.py`, `evaluate.py` theo thứ tự nếu file có mặt. Cần quy ước khác thì báo mình sửa.

## Kết quả chạy thử trên khoảng của Bảo

Đầu vào:
- bảng `bibtex` bản M2 đã chốt (tag `m2-final`, trong thư mục `g11_m2_benchmark`);
- references từ bản cào M1 ngày 01/11 (`MS2_input/GBao_MS2.tar.gz`). Bản M2 chốt đọc bản cào mới hơn, nên số liệu chỉ để thử.

| | |
|---|---|
| Entry | 223.233, thuộc 4.709 bài |
| Reference có arXiv ID | 92.916, thuộc 4.338 bài |
| Bài có cả entry lẫn reference | 4.130 |
| Bài có entry nhưng không có reference (mọi entry là "không có") | 579 |
| Cặp ứng viên | 5.831.166 (mỗi bài trung vị 646, m trung vị 40, n trung vị 17) |
| Bài có m ≥ 20 và n > 0 (đủ để gán tay) | 3.531 |

Luật đơn giản nhất trên dữ liệu đã làm sạch (gợi ý cho Member 2, không phải nhãn) đã nối được 39,3% entry:
- cùng arXiv ID: 33.345 entry;
- không cùng ID nhưng cùng DOI (tính cả DOI arXiv): 16.148 entry;
- không cùng ID, DOI nhưng `title_clean` giống hệt: 38.283 entry.

Phần còn lại có tiêu đề gần giống, hoặc thật sự không có đáp án.

## Lỗi dữ liệu thấy được (chỉ báo, không sửa)

`m3_data.py` in các dòng `!!` mỗi lần chạy. Trên khoảng của Bảo:

- **Bảng `bibtex` (M2):**
  - 10.400 tên tác giả có dấu `\` thừa ở cuối (`Yao, D.-X. \`); làm sạch tự bỏ đi.
  - 18 chuỗi tác giả chứa nhiều người (`E. Özkan, H. Aydın, R. Dikici`); làm sạch tách ra.
  - 6 tên mất dấu do M2 (`Ir {s}i {c}, V.`, gốc là `Iršič`); họ ra sai, không cứu được.
  - 6.857 entry không có tiêu đề (3,1%), 1.633 entry không có tác giả, 2.289 entry không có năm.
- **References (M1, Semantic Scholar):**
  - Danh sách reference không đủ: 579 bài có entry nhưng không có reference arXiv nào. Có entry ghi rõ arXiv ID mà ID đó không nằm trong danh sách reference của bài.

    Kỹ thuật ghép không cứu được những trường hợp này; muốn bù thì phải thêm dữ liệu vào bảng `references` của M1, nhóm quyết.
  - Năm nộp arXiv v1 và năm Semantic Scholar lệch nhau ở 1.139 reference.

## Cho các bạn khác

- **Member 2 (gán nhãn):**
  - Lấy `paper_stats.csv` để chọn bài.
  - Luật tự động đọc `arxiv_id_in_entry`, `doi`, `title_clean`, `year`, `first_author_last` ở cả hai bảng. Nhớ so DOI arXiv như ở mục 2.
  - Công cụ gán tay hiện `title`, `authors`, `url` (cột gốc).
- **Member 3 (đặc trưng):**
  - Tính trên `candidates.parquet`, nối với hai bảng sạch theo khoá.
  - `title_tokens` cho Jaccard/TF-IDF, `title_clean` cho Levenshtein và token-set ratio.
  - `authors_last` cho Jaccard họ; `year` và `year_s2` cho độ lệch năm.
- **Member 4:** `splits.json` theo đúng khoá bài; bảng `predictions` lấy `citation_key` từ `entries_clean.parquet`.
- **Member 5:** bảng `references` lên Hugging Face lấy đúng các cột gốc của `references_clean.parquet` (`arxiv_id, ref_arxiv_id, ref_title, ref_authors, ref_submission_date, ref_s2_id`). Cột làm sạch chỉ dùng nội bộ.
