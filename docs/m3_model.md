# M3 – Model, đánh giá và bảng predictions

Code nằm trong `src/m3/modeling/`. Phần phân tích có biểu đồ nằm trong notebook `src/m3/notebooks/m3_features_model.ipynb`. Đặc trưng được mô tả trong `docs/m3_features.md`.

```bash
python src/m3/modeling/train.py    --work data/m3_all                 # chọn model (CV trên train), train model cuối
python src/m3/modeling/ablation.py --work data/m3_all                 # ablation theo nhóm + permutation importance (CV)
python src/m3/modeling/evaluate.py --work data/m3_all                 # MRR@5 trên valid
python src/m3/modeling/evaluate.py --work data/m3_all --part test     # test: chỉ chạy MỘT LẦN, ở cuối
python src/m3/modeling/predict.py  --work data/m3_all                 # bảng predictions
python src/m3/modeling/errors.py   --work data/m3_all                 # phân tích lỗi
python src/tests/m3/test_modeling.py
```

`pipeline_m3.py` gọi lần lượt `modeling/train.py`, `predict.py`, `evaluate.py`. Cần cài `numpy`, `scikit-learn`, `lightgbm` (và `pandas`, `matplotlib` để chạy notebook), đã có trong `src/requirements.txt`. Không gọi API ngoài, không dùng model pretrained.

| File | Việc |
|---|---|
| `pairs.py` | đọc `features.parquet` + `splits.json`; bộ đặc trưng (`DROPPED`, `COMPACT`); che link; MRR@5, Hit@1, Hit@5 |
| `models.py` | 2 baseline, Logistic Regression, LightGBM; lưu/nạp model |
| `train.py` | cross-validation 5 fold theo bài trên train; chọn model; train model cuối trên toàn train |
| `ablation.py` | ablation theo nhóm và permutation importance (CV) |
| `evaluate.py` | MRR@5 trên valid hoặc test |
| `predict.py` | bảng `predictions` |
| `errors.py` | phân tích lỗi; các cặp khớp mà luật auto có thể đã bỏ sót |

## 1. Các model so sánh

| Tên | Loại | Mô tả |
|---|---|---|
| `rules` | baseline | luật gán nhãn auto viết dưới dạng điểm: `4×id_match + 4×doi_match + 2×title_exact + title_jacc3` |
| `title` | baseline | chỉ độ giống title (`title_jacc3`) |
| `logreg_m00`, `logreg_m50` | phân loại cặp | Logistic Regression (điền trung vị, chuẩn hoá, cân bằng lớp) |
| `lgbm_m00`, `lgbm_m50` | **learning to rank** | LightGBM `lambdarank`, mỗi entry một nhóm, 300 cây, learning rate 0,05, không early stopping |

- **`_m00` / `_m50`:** tỉ lệ entry bị **che đặc trưng link** khi train. Che link nghĩa là `id_match`, `doi_match`, `entry_has_id`, `entry_has_doi` được gán 0, như thể entry không ghi ID/DOI. Mục đích là buộc model học khớp bằng title, tác giả, năm (chống "học đường tắt" qua ID).
- **Train:** chỉ trên sample (`is_sample = 1`) của tập train. Mỗi nhóm có 1 cặp đúng và n − 1 cặp sai.

## 2. Chọn model: vì sao dùng cross-validation trên train

Chia tập theo đúng yêu cầu: valid và test, mỗi thành viên góp 1 bài tay + 1 bài auto (`split.py --per-range`). Nhưng **valid quá dễ**: trên 400 entry của valid, **mọi** scorer (kể cả baseline "chỉ title") đều đạt MRR@5 = 1,000, nên không phân biệt được model nào.

Vì vậy model được so sánh bằng **cross-validation 5 fold trên train**:
- 62.866 entry, chia **theo bài**, nên một bài không bao giờ vừa ở phần train vừa ở phần chấm;
- valid và test **không** được dùng ở bước này.

Valid được báo cáo để kiểm tra. Test chỉ được chấm **một lần**, khi đặc trưng và model đã chốt.

**Ba con số theo dõi:**
- **all:** MRR@5 bình thường. Gần 1 với mọi model, vì phần lớn nhãn train do chính luật tạo ra (vòng tròn).
- **no link:** khi che link (giả lập entry không ghi ID/DOI).
- **hard, no link:** trên **3.119 entry khó** (cặp đúng không có title giống hệt) khi che link. Đây là con số trung thực nhất.

Model được chọn là model có trung bình cao nhất của ba con số này.

| Model (bộ full, 26 đặc trưng) | all | no link | **hard, no link** |
|---|---|---|---|
| rules | 1,0000 | 0,9934 | 0,8670 |
| title | 0,9934 | 0,9934 | 0,8670 |
| logreg_m00 | 1,0000 | 0,9986 | 0,9719 |
| logreg_m50 | 1,0000 | 0,9990 | 0,9800 |
| lgbm_m00 | 1,0000 | 0,9990 | 0,9795 |
| **lgbm_m50** | 0,9999 | 0,9993 | **0,9851** |

Với bộ compact (17 đặc trưng), `lgbm_m50` đạt 0,9999 / 0,9992 / 0,9849, gần như bằng bộ full, nên **model cuối là `lgbm_m50` trên bộ compact**. File model: `WORK/models/lgbm_m50.pkl`; kết quả CV: `WORK/train_report.json` (compact) và `train_report_full.json` (full).

## 3. Kết quả

**Valid** (400 entry): mọi scorer đạt 1,000.

**Test** (chấm một lần; 346 entry: 138 tay, 208 auto):

| | MRR@5 all | tay | auto | no link: all | no link: tay | Hit@1 tay |
|---|---|---|---|---|---|---|
| rules | 0,9986 | 0,9964 | 1,0000 | 0,9908 | 0,9843 | 0,9928 |
| title | 0,9908 | 0,9843 | 0,9952 | 0,9908 | 0,9843 | 0,9783 |
| **lgbm_m50 (model chọn)** | **1,0000** | **1,0000** | **1,0000** | 0,9986 | 0,9964 | 1,0000 |

## 4. Phân tích lỗi (`errors.py` → `errors.csv`, `errors_summary.json`)

| Nguồn | Entry | Baseline title sai | Model sai | Model sửa được | Model làm hỏng |
|---|---|---|---|---|---|
| test, bình thường | 346 | 4 | **0** | 4 | 0 |
| test, không link | 346 | 4 | 1 | 3 | 0 |
| train (out-of-fold), bình thường | 62.866 | 457 | **10** | 447 | 0 |
| train (out-of-fold), không link | 62.866 | 457 | **71** | 390 | 4 |

**Ví dụ model sửa được lỗi của baseline title:**
- `CoverUp: Coverage-Guided, LLM-Based Test Generation` ↔ `CoverUp: Effective High Coverage Test Generation for Python` (bài đổi tên);
- bản ICSE của ClassEval;
- entry có title là `"Folkestad"` (M2 parse lỗi) nhưng tác giả và năm khớp;
- reference mà S2 lưu title là `OUP accepted manuscript`.

**Lỗi còn lại của model** (không link, train) gồm 2 loại chính:
- **rewritten title** (35): title của entry gần như không liên quan đến title của reference. Nguyên nhân là S2 lưu title rác (`OUP accepted manuscript`, mã MathML, `\documentclass…`), hoặc M2 lấy nhầm tên tác giả, tạp chí hay arXiv ID làm title (`"arXiv:2411.17604"`, `"Abdullaev"`).
- **missing information** (32): entry không có title. Khi đó chỉ còn tác giả và năm, mà không đủ để phân biệt các bài **cùng tác giả, cùng năm**.

Hầu hết lỗi bắt nguồn từ **dữ liệu**, không phải model. Có thể giảm bằng cách làm sạch title ở M2 (lỗi parse) và ở M3 (bỏ MathML/LaTeX trong title của S2).

## 5. Hạn chế: vì sao MRR ≈ 1

1. **Nhãn auto là vòng tròn.** 2.500/2.525 bài có nhãn do luật tạo ra (trùng ID, trùng DOI, title giống hệt), mà model dùng chính các tín hiệu đó. Baseline `rules` cũng đạt khoảng 1.
2. **Bài tay thiên về bài dễ.** Để đạt ≥ 20 cặp khớp mỗi bài, chỉ chọn bài mà S2 liên kết tốt.
3. **Bài toán dễ trên dữ liệu này.** Mỗi entry chỉ so với khoảng 40 reference của chính bài, và người ta thường trích gần đúng nguyên title.
4. **Entry không khớp không được chấm** (định nghĩa |Q| của đề). Phần khó nhất là nhận ra entry không có trên arXiv, và phần đó không được tính.
5. **Luật bỏ sót cặp khớp, chủ yếu là ca khó.** Recall của luật là 0,987, đo trên nhãn tay độc lập. Trong các entry "không khớp" ở bài auto, có khoảng **427–764** entry mà ứng viên tốt nhất trông như cùng một bài (title ≥ 0,5–0,8 và cùng tác giả đầu); ở bài tay chỉ có 2–4 entry như vậy. Ví dụ: title 2 từ (`Inception transformer`, `Machine unlearning`), năm lệch 2, title bị lẫn tên tác giả hoặc năm, bài đổi tên. Các ca này không trở thành sample, nên dữ liệu lệch về phía dễ.
   - **Không sửa luật**, vì nới luật sẽ kéo theo các ca "giống mà khác" (`Qwen2.5` ↔ `Qwen3`, `Langlands I` ↔ `IV`), và nhãn tay hầu như không có loại ca này để đo precision. Danh sách nằm ở `WORK/missed_matches.csv`.

Vì vậy con số MRR@5 ≈ 1 trên test cho thấy **pipeline đúng**, chứ chưa phản ánh năng lực model. **Năng lực thật** đo trên **ca khó khi không link**: baseline 0,867 so với LightGBM 0,985; số entry xếp sai giảm từ 457 xuống 71.

## 6. Bảng predictions (`WORK/predictions.parquet`)

Một dòng cho **mọi** BibTeX entry của các bài dùng cho matching (train + valid + test), gồm 126.065 dòng của 2.525 bài. Các cột theo đề:

| Cột | Nội dung |
|---|---|
| `arxiv_id` | bài chứa entry |
| `partition` | `train` / `valid` / `test` (từ `splits.json`) |
| `citation_key` | entry |
| `groundtruth_arxiv_id` | reference đúng; null nếu entry không khớp (không phải sample) |
| `cand_1` … `cand_5` | 5 ứng viên tốt nhất theo model (cand_1 tốt nhất); null nếu bài có dưới 5 ứng viên |

Thêm cột `range` (khoảng ID của thành viên) để mỗi người lọc phần của mình khi đưa lên Hugging Face cá nhân. Bỏ cột này khi đăng nếu cần giữ đúng schema.
