# M3 – Đặc trưng (feature engineering)

Đầu vào: `labels.csv` (xem `docs/m3_labeling.md`), cùng `entries_clean.parquet` và `references_clean.parquet` (xem `docs/m3_member1.md`). Phần phân tích có biểu đồ nằm trong notebook `src/m3/notebooks/m3_features_model.ipynb`.

```bash
python src/m3/features.py --work data/m3_all                       # -> features.parquet, features.json
python src/m3/feature_report.py --work data/m3_all --part train    # -> feature_report_train.csv
python src/tests/m3/test_features.py
```

## 1. Cách đặt bài toán: learning to rank

Mỗi BibTeX entry là một **nhóm**. Ứng viên của nó là các reference có arXiv ID của **chính bài đó**, tức một dòng của ma trận m×n trong đề.
- Model chấm điểm từng cặp (entry, ứng viên), sắp xếp, rồi lấy top 5. Như vậy khớp trực tiếp với MRR@5.
- Đặc trưng được tính trên **từng cặp**, nên cùng một bảng dùng được cho cả model xếp hạng (LightGBM `lambdarank`, model chính) lẫn model phân loại cặp (Logistic Regression, baseline).

**Sample.** Theo giải đáp của thầy, một nhãn là **một cặp khớp**. Vì vậy chỉ entry có `is_match = true` mới là sample. Mỗi sample cho một nhóm gồm 1 cặp `y = 1` (reference đúng) và n − 1 cặp `y = 0`. Entry "không khớp" vẫn có dòng (`is_sample = 0`), nhưng chỉ để model xếp hạng cho bảng predictions; chúng không dùng để train, không dùng để chấm, và **không bao giờ** bị coi là mẫu âm.

Quy mô `features.parquet`: 4.636.680 cặp, 126.065 entry, trong đó 63.612 là sample.

## 2. Các đặc trưng (31, chia 6 nhóm)

Ký hiệu: E là entry, R là reference ứng viên. Title đã được làm sạch: bỏ LaTeX, bỏ dấu, chữ thường, bỏ dấu câu. Tác giả so theo **họ**. NaN nghĩa là không tính được, ví dụ do thiếu title.

| Nhóm | Đặc trưng | Cách tính | Ý tưởng |
|---|---|---|---|
| link | `id_match` | arXiv ID ghi trong E (url/journal/doi/title) = ID của R | bằng chứng gần như chắc chắn, nhưng chỉ khoảng 37% entry có ID |
| | `doi_match` | DOI của E = DOI của R, hoặc = `10.48550/arxiv.<ID>` | như trên, cho DOI |
| | `entry_has_id`, `entry_has_doi` | E có ghi ID / DOI không | cho model biết "không trùng ID" là do thiếu ID hay do khác bài |
| | `entry_id_in_paper`, `entry_doi_in_paper` | ID / DOI của E có khớp reference nào trong bài không | *bị bỏ: trùng với `entry_has_*`* |
| title | `title_jacc3` | Jaccard các 3-gram ký tự của title đã bỏ khoảng trắng | chịu được viết dính/tách (`M 2 diffuser` = `M2Diffuser`), lệch ít chữ thì mất ít |
| | `title_jacc_word` | Jaccard tập từ (bỏ stop-word) | đơn giản; nhạy với cách tách từ |
| | `title_tfidf` | cosine TF-IDF; IDF tính từ mọi title trong corpus của nhóm | từ hiếm có trọng số cao hơn từ phổ biến |
| | `title_exact`, `title_contain` | title giống hệt; title này chứa title kia | tín hiệu rõ; bắt title bị cắt hoặc thêm phụ đề |
| | `title_len_ratio` | độ dài ngắn / độ dài dài | phân biệt "X" với "Improved X" |
| | `entry_title_missing` | E không có title | báo cho model phải dựa vào tác giả và năm |
| author | `first_author_match` | họ tác giả đầu của E nằm trong các họ của R | trích dẫn thường giữ đúng tác giả đầu |
| | `author_jacc`, `author_overlap` | Jaccard và số họ trùng | phân biệt bài cùng title nhưng khác tác giả |
| | `entry_authors_missing`, `entry_authors_group` | E thiếu tác giả; tác giả chỉ là "et al." hoặc tổ chức | khi đó không trùng họ là bình thường |
| year | `year_gap` | \|năm E − năm R\| nhỏ nhất (R có năm arXiv và năm S2) | bài đúng thường lệch 0–1 năm (preprint so với bản đăng) |
| | `year_gap_signed` | như trên, có dấu | *bị bỏ: cặp đúng và cặp sai có cùng trung bình* |
| | `year_missing` | thiếu năm | cờ |
| venue | `venue_sim` | 3-gram Jaccard giữa journal/booktitle của E và venue của R | tín hiệu phụ |
| | `entry_is_preprint` | E là preprint arXiv | *bị bỏ: trùng với `entry_has_id`* |
| | `entry_type_code` | mã của `entry_type` | book/misc ít khi có trên arXiv |
| context | `n_candidates` | số ứng viên của bài | |
| | `title_rank`, `tfidf_rank`, `author_rank` | thứ hạng của R trong các ứng viên của E, theo từng độ giống | bài toán xếp hạng nên cần so **tương đối**: 0,6 có thể là cao nhất ở bài này nhưng kém ở bài khác |
| | `title_gap_best`, `title_margin_second` | khoảng cách tới ứng viên tốt nhất; độ dẫn của ứng viên tốt nhất so với ứng viên thứ hai | độ chắc chắn của title |
| | `title_is_best` | R có title giống nhất | *bị bỏ: trùng với `title_rank = 1`* |

**Chống rò rỉ.** Không đặc trưng nào lấy từ nhãn, tên luật gán nhãn (`note`) hay `rule_hint`. Đặc trưng "context" chỉ so R với các ứng viên **khác của cùng entry**; lúc dự đoán thật cũng có đủ các ứng viên này. **External knowledge:** không dùng. Không tải model hay corpus ngoài; TF-IDF được fit trên title của chính corpus nhóm (theo giải đáp của thầy, như vậy không tính là external).

## 3. Phân tích trên train (`feature_report_train.csv`)

Tính trên 62.866 sample của tập train, không nhìn test.

| Đặc trưng | yes / no | MRR@5 khi dùng riêng |
|---|---|---|
| title_jacc3 / title_jacc_word / title_tfidf | 0,99 / 0,05–0,08 | 0,993 / 0,993 / 0,992 |
| title_exact / title_contain | 0,95–0,97 / 0,00 | 0,950 / 0,954 |
| author_jacc / author_overlap | 0,92 / 0,03 · 5,7 / 0,3 | 0,944 / 0,894 |
| first_author_match | 0,99 / 0,06 | 0,597 |
| id_match / doi_match | 0,38 / 0,00 · 0,33 / 0,00 | 0,376 / 0,336 |
| year_gap (năm) | 0,64 / 4,9 | 0,225 |
| venue_sim | 0,40 / 0,09 (thiếu 20%) | 0,253 |
| title_rank, title_gap_best, tfidf_rank, author_rank | | 0,992–0,993; author_rank 0,944 |

- "yes" / "no" là trung bình ở cặp đúng / cặp sai. Với đặc trưng 0/1, trung bình chính là tỉ lệ.
- Đặc trưng cấp entry (cờ thiếu, `entry_has_*`, `entry_type_code`, `n_candidates`) giống nhau cho mọi ứng viên của cùng entry. Vì vậy MRR khi dùng riêng của chúng ≈ 0; chúng chỉ có ích khi kết hợp với đặc trưng khác.
- **Tương quan > 0,9:**
  - `entry_has_id` ≈ `entry_id_in_paper` (1,00) ≈ `entry_is_preprint` (0,98);
  - `entry_has_doi` ≈ `entry_doi_in_paper` (0,94);
  - ba độ giống title (0,92–0,96);
  - `title_exact` ≈ `title_contain` ≈ `title_is_best` ≈ `title_margin_second` (0,95–0,98).

## 4. Chọn đặc trưng: 31 → 26 → 17

1. **Bỏ 5 đặc trưng trùng lặp hoặc vô ích** (mục 3): `entry_id_in_paper`, `entry_doi_in_paper`, `entry_is_preprint`, `year_gap_signed`, `title_is_best`. Còn **26** đặc trưng, gọi là bộ "full".
2. **Ablation theo nhóm và permutation importance** (`src/m3/modeling/ablation.py`, cross-validation 5 fold theo bài trên train; xem `docs/m3_model.md` về lý do không dùng valid). Số đo chính là MRR@5 trên **3.119 ca khó** (cặp đúng không có title giống hệt) khi **che link**. Model đầy đủ đạt 0,9851.

| Bỏ nhóm | MRR@5 | giảm |
|---|---|---|
| title + context (cùng mang thông tin về title) | 0,9548 | **−0,030** |
| year | 0,9778 | −0,007 |
| author | 0,9809 | −0,004 |
| venue | 0,9842 | −0,001 |
| chỉ title / chỉ context / link | ≈ 0,985 | ≈ 0 (nhóm còn lại bù vào; link đã bị che) |

Permutation importance (khi che link, trên ca khó) xếp đầu: `year_gap`, `author_rank`, `venue_sim`, `author_jacc`, `first_author_match`, `title_gap_best`. Các đặc trưng title riêng lẻ có importance ≈ 0, vì chúng bù cho nhau.

3. **Bộ "compact" (17 đặc trưng)**, model cuối dùng bộ này:
   - **link:** `id_match`, `doi_match`, `entry_has_id`, `entry_has_doi`
   - **title:** `title_jacc3`, `title_tfidf`, `entry_title_missing`
   - **author:** `first_author_match`, `author_jacc`, `author_overlap`, `entry_authors_missing`, `entry_authors_group`
   - **year:** `year_gap`, `year_missing`
   - **venue:** `venue_sim`
   - **context:** `title_gap_best`, `author_rank`

   CV cho thấy compact bằng full: ca khó khi không link 0,9849 so với 0,9851, chênh chưa tới 1 entry trên 3.119. Ít hơn 9 đặc trưng, dễ giải thích hơn, nên chọn compact.

Danh sách nằm trong `src/m3/modeling/pairs.py` (`DROPPED`, `COMPACT`). `features.parquet` vẫn giữ đủ 31 cột.

## 5. Tóm tắt

- **Title là tín hiệu mạnh nhất:** riêng nó đã cho MRR khoảng 0,99 trên train.
- **Tác giả và năm phân xử** các bài trùng tên hoặc gần trùng tên. Ví dụ "Generative adversarial networks" của Goodfellow và bài tổng quan cùng tên của Cohen; DDPM và Improved DDPM.
- **Link gần như không bao giờ sai** nhưng chỉ có ở khoảng 1/3 số entry. Vì vậy khi train, link được che ở 50% entry để model không phụ thuộc vào nó (xem `docs/m3_model.md`).
