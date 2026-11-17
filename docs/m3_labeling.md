# M3 – Gán nhãn: chọn bài gán tay và file gán tay

Hai bước đầu của phần gán nhãn, chạy sau `m3_data.py` và `candidates.py` của Member 1 (xem `docs/m3_member1.md`):

1. `src/m3/select_manual.py`: chọn bài để gán tay, **5 bài mỗi khoảng ID**.
2. `src/m3/label_sheet.py`:
   - `export`: xuất file Excel để người gán điền;
   - `import`: kiểm tra file đã điền rồi ghi ra file nhãn.

Chỉ cần `pyarrow`. Muốn xuất ra `.xlsx` thì cần thêm `openpyxl` (`pip install openpyxl`); không có thì script tự xuất CSV.

## Thầy đã chốt gì (giải đáp Q&A)

| Câu hỏi | Thầy trả lời | Ảnh hưởng tới code |
|---|---|---|
| Nhãn là gì | Một **nhãn = một cặp (entry, reference) khớp**. Entry không có reference nào thì **không phải nhãn**. | Bài gán tay phải có **≥ 20 cặp khớp**, mỗi thành viên **≥ 100 cặp**. Entry "không có" không được tính vào các con số này. |
| MRR@5, \|Q\| | Entry không có reference **không phải sample hợp lệ**. Q chỉ gồm các nhãn matching. | Khi đánh giá chỉ dùng các dòng `is_match = true`. Dòng toàn "no" không nằm trong \|Q\|. |
| Bài gán tay và luật tự động | Bài đã gán tay thì **không được** đưa qua pipeline gán tự động. | Luật auto phải loại các bài trong `manual_pick.csv`. |
| Gán tự động | Dùng **thuật toán matching định ra từ trước** (luật), không dùng model nhóm train. **10% tính theo số bài**: mỗi thành viên khoảng 500 bài, cả nhóm khoảng 2.500. | Viết `auto_label.py` bằng luật. |
| Chia tập | **Gộp chung cả nhóm**: số bài tay và tự động tính theo số thành viên, rồi gộp thành một bộ train/valid/test. Test có 5 tay + 5 tự động, valid cũng vậy. | `split.py` của Quân làm đúng. |
| Ứng viên | Lấy từ references **của chính bài đó**. Bài có dưới 5 ứng viên thì các ô `cand_*` còn thiếu để **null**. | |
| External knowledge | Chỉ tính là external khi phải gọi API ngoài, hoặc tải corpus hay pretrained model từ bên ngoài, lúc chạy trên Colab. TF-IDF, Word2Vec tự train trên corpus của nhóm, LR/RF/LightGBM đều **không** phải external. | |
| Định dạng nhãn trung gian | Nhóm tự quyết. Chỉ chấm các bảng Parquet trên Hugging Face. | |
| Nộp | Pipeline dùng chung. Mỗi thành viên trích **phần dữ liệu thuộc khoảng ID của mình** và host trên **Hugging Face cá nhân**. | |

## Chạy

```bash
# 1. chọn 5 bài cho mỗi khoảng có trong dữ liệu -> WORK/manual_pick.csv
python src/m3/select_manual.py --work data/m3_work --data-dir data/m1

# 2a. xuất file để gán -> WORK/label_sheet_<tên>.xlsx
python src/m3/label_sheet.py export --work data/m3_work --picks data/m3_work/manual_pick.csv --labeler Bao

# (điền file Excel)

# 2b. kiểm tra và ghi nhãn -> WORK/labels_manual_<tên>.csv
python src/m3/label_sheet.py import --work data/m3_work --sheet data/m3_work/label_sheet_Bao.xlsx --labeler Bao

python src/m3/test_m3_labeling.py
```

Khi chạy cho cả nhóm, truyền mọi thư mục M1 vào `--data-dir`. Mỗi người dùng `export --range <tên khoảng>` để lấy riêng bài của khoảng mình.

## Bước 1: chọn bài như thế nào

**Luật khớp chính xác.** Đây là cách ghép tự động đơn giản nhất: entry trỏ tới một reference của cùng bài khi có cùng arXiv ID, cùng DOI (kể cả DOI arXiv `10.48550/arxiv.<id>`), hoặc `title_clean` giống hệt. Từ luật này tính được:

- `rule_matched`: số entry mà luật khớp được.
- `sure_pairs`: số reference **khác nhau** mà luật tìm được. Đây là **cận dưới** của số nhãn bài sẽ có; người gán thường tìm thêm được những cặp luật bỏ sót.
- `rule_share` = `rule_matched / m`: tỉ lệ entry đã được luật giải quyết. Phần còn lại là chỗ người gán phải tự phán đoán: hoặc một cặp luật bỏ sót, hoặc entry không có reference nào.

**Bài hợp lệ.** Ngưỡng nào cũng đổi được bằng tham số dòng lệnh.

| Điều kiện | Mặc định | Vì sao |
|---|---|---|
| Trạng thái Semantic Scholar | `success` | danh sách reference đáng tin |
| số entry m | 25 ≤ m ≤ 100 | đủ entry, và không quá dài để xem hết |
| số reference có arXiv ID n | n ≥ 25 | đủ chỗ cho 20 cặp khớp (số nhãn không thể vượt n) |
| `sure_pairs` | ≥ 10 | chắc chắn có sẵn một nửa số nhãn cần, tránh chọn phải bài không đạt nổi 20 nhãn |
| entry không có title | ≤ 2 | entry thiếu title gần như không gán được |
| entry `misc` | ≤ 30% | phần lớn misc là website hoặc phần mềm |

Trên khoảng 2604.05535–2604.10534 có 1.142 bài hợp lệ.

**Độ khó** được tính tương đối trong từng khoảng: xếp các bài hợp lệ theo `rule_share` rồi chia làm ba phần bằng nhau. Một phần ba bài có `rule_share` cao nhất là *dễ*, một phần ba thấp nhất là *khó*.

Không dùng `sure_pairs / n` để đo độ khó, vì trên khoảng của Bảo luật gần như đã tìm được mọi reference arXiv của bài hợp lệ: trung vị 1,0, phân vị 10% là 0,93. Con số này gần như bằng nhau ở mọi bài nên không phân biệt được bài dễ với bài khó.

**Rút bài.** Mỗi khoảng rút 5 bài: **1 dễ, 2 vừa, 2 khó**, rút ngẫu nhiên trong từng mức với seed cố định (`--seed 11`, cùng lệnh thì ra cùng kết quả). Nếu cả 5 bài đều là cs mà khoảng có bài hợp lệ ngoài cs, bài cuối được đổi sang một bài cùng mức ngoài cs.

Vì sao chọn như vậy:
- Nếu chỉ toàn bài dễ, nhãn tay sẽ trùng với nhãn của luật tự động, và MRR trên test đẹp giả.
- Trộn lĩnh vực để không chỉ đánh giá trên bài ML.

`--exclude FILE` bỏ qua các bài đã dùng, ví dụ khi cần chọn thêm.

`manual_pick.csv` có các cột: `range, arxiv_id, primary_category, m, n, rule_matched, sure_pairs, rule_share, difficulty, reason`.

## Bước 2: file gán tay

**Sheet `entries`.** Mỗi dòng là một entry của bài đã chọn, lấy **mọi entry** của bài, sắp theo bài rồi theo `citation_key`.

| Cột | Là gì |
|---|---|
| `arxiv_id` … `arxiv_id_in_entry` | entry như M2 ghi (title, authors, year, journal, url, doi) |
| `rule_hint` | luật khớp chính xác nối entry với reference nào (`same DOI -> 2504.13554`); trống nếu không có luật nào |
| **`label_ref_arxiv_id`** | **người gán điền**: `ref_arxiv_id` đúng; **để trống = không có reference nào của bài là entry này** |
| **`checked`** | **người gán điền `x`** khi đã quyết xong dòng (kể cả khi để trống) |
| **`note`** | ghi chú tuỳ ý |
| `cand_1_*` … `cand_5_*` | 5 reference của cùng bài giống entry nhất: ID, title, tác giả, năm, điểm |

**Điểm gợi ý** chỉ dùng để sắp thứ tự, người gán vẫn tự quyết:
- `0,6 × độ giống title` (difflib trên các từ đã sắp xếp)
- `+ 0,3 × Jaccard họ tác giả`
- `+ 0,1 × độ gần năm` (cùng năm 1, lệch một năm 0,5)

Reference mà luật cùng arXiv ID hoặc cùng DOI chỉ ra được đưa lên đầu (điểm 1).

**Sheet `references`.** Mọi reference của các bài đã chọn, để tra khi không gợi ý nào đúng.

**Sheet `README`.** Hướng dẫn điền.

**Vì sao cần cột `checked`:** ô `label_ref_arxiv_id` trống vừa có thể là "không khớp", vừa có thể là "chưa xem". Cột `checked` để phân biệt hai trường hợp này.

**Vì sao không điền sẵn nhãn:** nhãn tay là thước đo để đánh giá luật tự động (precision/recall) và model. Nếu điền sẵn theo luật thì thước đo bị lệch theo luật. `rule_hint` chỉ là gợi ý, nên ghi trong poster là người gán có xem gợi ý.

## Định dạng file nhãn

`import` ghi `labels_manual_<tên>.csv`, **mỗi dòng là một entry đã xem** (đã có `x`):

| Cột | Là gì |
|---|---|
| `arxiv_id`, `citation_key` | entry (khoá) |
| `label_ref_arxiv_id` | reference đúng; trống nếu entry không có reference |
| `is_match` | `true` khi có reference: **chỉ dòng này là nhãn** (tính vào 20/100, vào \|Q\| và vào train) |
| `source` | `manual` (luật tự động sẽ ghi `auto`) |
| `labeler` | người gán |
| `note` | ghi chú |

Dòng `is_match = false` vẫn được giữ trong file để biết entry đó đã được xem và quyết định là "không có", nhưng các bước sau (đặc trưng, train, đánh giá MRR) **chỉ dùng dòng `is_match = true`**.

`split.py` đọc cột `arxiv_id` và `source`; `snapshot_diff.py` đọc `citation_key` và `label_ref_arxiv_id`. Nhãn của cả nhóm gộp lại bằng cách nối các file CSV.

`import` từ chối ghi file (mã thoát 1) và liệt kê lỗi khi:
- có dòng chưa có `x` ở `checked`;
- `label_ref_arxiv_id` không phải reference của chính bài đó;
- một entry xuất hiện hai lần;
- một bài có ít hơn `--min-labelled` (mặc định 20) **cặp khớp**.

Ngoài ra `import` còn cảnh báo (vẫn ghi file) khi:
- người gán có dưới 100 cặp khớp;
- một reference là nhãn của nhiều entry trong cùng bài (thường là cùng một tài liệu bị trích hai lần, cần xem lại).
