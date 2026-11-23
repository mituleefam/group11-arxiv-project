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

python src/tests/m3/test_manual_labeling.py
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

## Bước 3: gán nhãn tự động (`auto_label.py`)

```bash
# gán tự động -> WORK/labels_auto.csv
python src/m3/auto_label.py --work data/m3_all

# đo chất lượng luật trên nhãn tay (chỉ để đo, không ghi nhãn)
python src/m3/auto_label.py --work data/m3_all --evaluate data/m3_all/labels_manual_*.csv --errors data/m3_all/auto_vs_manual.csv

python src/tests/m3/test_auto_label.py
```

**Chọn bài (pool auto).** Theo thầy, 10% tính theo **số bài** ngoài phần gán tay, cho mỗi thành viên: ceil(0,10 × (5000 − 5)) = **500 bài mỗi khoảng**, cả nhóm 2.500 bài.

- Các bài trong `manual_pick.csv` **không bao giờ** được rút vào pool auto.
- Chỉ rút từ bài có trạng thái S2 `success`, m ≥ 1 và n ≥ 5. Bài không có reference thì không sinh được nhãn, còn bài auto vào test/valid thì cần có ứng viên để xếp hạng.
- Rút ngẫu nhiên với seed cố định theo từng khoảng.
- Muốn lấy nhiều hơn thì dùng `--fraction`.

**Luật** (định sẵn từ trước, không phải model). Các luật chạy trong một bài, theo thứ tự:

| Luật | Điều kiện |
|---|---|
| R1 | trùng arXiv ID |
| R2 | trùng DOI, kể cả DOI arXiv `10.48550/arxiv.<id>` |
| R3 | `title_clean` trùng đúng một reference, title ≥ 3 từ, và (năm lệch ≤ 1 hoặc tác giả trùng); title ≤ 5 từ thì tác giả hai bên không được mâu thuẫn |
| R4 | title giống ≥ 0,85, hơn reference giống thứ hai ≥ 0,10, tác giả trùng (họ tác giả đầu, hoặc ≥ một nửa các họ), năm lệch ≤ 1 |

**Xử lý trường hợp nhập nhằng**

*(a) Các luật mâu thuẫn trên một entry.* R1–R3 chỉ ra hai reference khác nhau → không gán nhãn, `note` ghi `conflict`.

*(b) Kiểm tra lại nhãn R1/R2 (`id_mismatch`).* arXiv ID/DOI do tác giả ghi **cũng có thể sai**: tác giả dán nhầm ID của một bài khác, thường là bài khác của cùng nhóm, hoặc DOI bị gắn nhầm bài. Vì vậy nhãn R1/R2 bị bỏ (`note` ghi `id_mismatch …`) khi:
- entry có **title đầy đủ**, **và**
- title của entry giống title reference **< 0,15**, **và** thoả một trong hai điều kiện:
  - tác giả không trùng, **hoặc**
  - có một entry khác cùng bài khớp reference đó **theo title** (độ giống ≥ 0,85). Khi đó entry kia mới là bài đó, và nhãn được giao cho entry kia.

  Trong trường hợp có entry khác như vậy, ngưỡng được nới lên **< 0,30 nếu tác giả không trùng**. Lý do: mọi bài đổi tên đã gặp đều giữ nguyên tác giả. Ví dụ bắt được nhờ nới ngưỡng: `autobio2024` ("Autobio: A benchmark for robotic biology labs", tác giả "Team AutoBio") mang ID của Chemistry3D (Li, Huang…), độ giống 0,21.

"Title đầy đủ" nghĩa là ≥ 4 từ và không phải mảnh vỡ do M2 đọc hỏng entry. Các mảnh bị loại: có chữ `arxiv`; tham chiếu tạp chí có số trong ≤ 8 từ (`Phys. A 20 3765`, `Chaos Solitons Fractals 202`); phần lớn là chữ viết tắt tên (`M. E. J. Newman`, `He and X. D. Ji`). Entry có title hỏng như vậy thì không bị nghi, vì ID/DOI của nó vẫn đúng.

Vì sao ngưỡng chỉ là 0,15: **bài đổi tên giữa các phiên bản arXiv** vẫn giữ ID nhưng title khác hẳn, và độ giống của chúng trải từ 0,16 đến 1,0. Ví dụ: MonST3R 0,16; Emergent Misalignment 0,21 (bản Nature đổi tên); MathVista 0,41; PlanBench 0,54 (bản arXiv đầu tên là "LLMs Still Can't Plan"). Trong khi đó, các ID bị dán nhầm có độ giống 0,07–0,21. Hai khoảng chồng nhau, và tác giả cũng không tách được hai loại (ID dán nhầm thường là của cùng nhóm tác giả). Vì vậy chọn ngưỡng thấp, chấp nhận còn sót vài ID sai để không bỏ nhầm bài đổi tên.

*(c) Nhiều entry cùng trỏ một reference → giữ tất cả, `note` ghi `shared`.* Đây là cùng một bài được trích bằng nhiều key: .bib trùng, có cả bản preprint lẫn bản đăng, hoặc một entry bị M2 đọc hỏng. Mỗi entry là một nhãn.

Bản đầu xử lý (c) khác: nhãn R3/R4 tranh reference với nhãn R1/R2 thì bị bỏ. Mình đã đọc lại **102 ca bị bỏ** như vậy:

| Loại | Số |
|---|---|
| entry bị bỏ thực ra đúng: cùng bài được trích hai lần | 66 |
| entry bị bỏ thực ra đúng: entry R1/R2 có title bị M2 đọc hỏng | 12 |
| entry bị bỏ thực ra đúng: entry R1/R2 là bản arXiv cũ, đã đổi tên | 15 |
| entry bị bỏ đúng, còn **entry R1/R2 mới là cái sai** (ID dán nhầm) | ~6 |
| chưa chắc | ~4 |

Vì vậy đã đổi sang cách (b) + (c).

`labels_auto.csv` cùng định dạng với nhãn tay (`source = auto`, `note` = tên luật).

**Kết quả trên dữ liệu cả nhóm** (`data/m3_all`, seed 11): 2.500 bài, 124.456 entry, **62.601 nhãn**. Trung vị 20 nhãn mỗi bài (trung bình 25, tối đa 300); 7 bài không có nhãn nào.

| Khoảng | Bài | Entry | Nhãn | R1 | R2 | R3 | R4 |
|---|---|---|---|---|---|---|---|
| Tram | 500 | 24.848 | 12.752 | 4.987 | 2.404 | 5.215 | 146 |
| Thu | 500 | 25.562 | 12.758 | 5.217 | 2.650 | 4.772 | 119 |
| Phat | 500 | 26.313 | 12.407 | 4.327 | 2.528 | 5.415 | 137 |
| Bao | 500 | 24.752 | 12.344 | 4.436 | 2.429 | 5.348 | 131 |
| Quan | 500 | 22.981 | 12.340 | 4.556 | 2.058 | 5.570 | 156 |
| **Tổng** | **2.500** | **124.456** | **62.601** | **23.523** | **12.069** | **26.320** | **689** |

Ngoài số nhãn ở bảng trên:
- 388 nhãn mang ghi chú `shared`.
- 9 entry bị bỏ vì các luật mâu thuẫn (`conflict`).
- 11 nhãn R1/R2 bị bỏ vì `id_mismatch`. Đọc tay cả 11 thì đều là ID/DOI sai thật:
  - 5 ID dán nhầm từ bài khác: `wang2024answers…`, `yang2025scalable…`, `jiang2023llmparser`, `autobio2024`, `mastqlp` (DOI của bộ dữ liệu);
  - 6 DOI trỏ sang bài khác hẳn: `Efstathiou2022`, `Farrah2022`, `Pfeifle2019b`, `white1992density`, `merloni_fabian`, `golden2008vehicle`.

So với bản trước, cả 102 entry từng bị bỏ đều đã được gán, và 11 nhãn sai bị loại; sau đó lần kiểm tra cuối (bên dưới) loại thêm 18 nhãn R3 sai: **62.528 → 62.601**.

**Kiểm tra cuối** (đọc tay, không đổi luật dựa trên một ca riêng lẻ):

1. **Mẫu ngẫu nhiên 40 nhãn** trên toàn bộ: cả 40 đều đúng.
2. **R3 với title ngắn và tác giả khác nhau.** Nhóm này có lỗi: những title chung chung trùng với một bài khác hẳn. Thêm điều kiện: title ≤ 5 từ thì tác giả hai bên không được mâu thuẫn. Tác giả là tổ chức (`Team Qwen`, `OpenAI`) và họ phiên âm khác (`Shanskii`/`Shansky`) không bị coi là mâu thuẫn. Kết quả loại 18 nhãn, đọc tay cả 18 đều sai:
   - 10 nhãn "Generative adversarial networks" (Goodfellow) bị nối nhầm vào bài tổng quan cùng tên của Cohen & Giryes (2022);
   - "Quantum phase transitions" (sách của Sachdev ↔ bài của Vojta, 2 nhãn);
   - "Gaia Data Release 2" (Brown ↔ Katz);
   - "The fractional quantum Hall effect" (Störmer, Tsui ↔ Rao);
   - "Self-guided masked autoencoder", "Retrieval Augmented Time Series Forecasting", "Interlayer pairing in bilayer nickelates", "Avoiding long Berge-cycles": mỗi title có hai bài khác nhau.
3. **R1/R2 có title rất khác reference** (title đầy đủ, độ giống < 0,3): có 152 nhãn. Tra **title v1 trên arXiv** của từng reference (API arXiv, chỉ dùng để kiểm tra, không dùng trong luật):
   - 104 nhãn có title entry giống title v1: là bài đổi tên, hoặc S2 lưu title lỗi (`OUP accepted manuscript`, mã MathML, `\documentclass…`). Các nhãn này **đúng**.
   - 35 nhãn trong đó M2 lấy nhầm nơi đăng, tên tác giả hoặc tên tạp chí làm title (`In Proceedings of the 2020 CHI Conference…`). ID/DOI của các nhãn này vẫn **đúng**.
   - **~10 nhãn sai thật**: tác giả dán ID/DOI của một bài khác **cùng nhóm**, ví dụ `rawat_rl`, `LMSWY2`, `Ball2012`, `Fiacconi2017`, `Cao2019`, `munshi_quantifying_2021`, `papadopoulos2014handbook`, `matsumoto_algebraic_2002`, `Lahiri2019`. Còn 1 nhãn trỏ vào bản ghi S2 rác (`Green:2004wb` → "paper added in error by admin team"). Luật **không bắt được** nhóm này: tác giả trùng và title khác hẳn, y như bài đổi tên.
   - ~3 nhãn chưa chắc: `brito-etal-2025-modeling`, `RW`, `Greener2025_PNAS…`.

**Ước lượng chất lượng:** đã biết khoảng 10–13 nhãn sai còn lại trên 62.601 nhãn (~0,02%). Với các nhóm chưa đọc hết thì ước lượng từ mẫu ngẫu nhiên (40/40 đúng). Không thể khẳng định là không còn nhãn sai nào. Bảng `labels_auto.csv` nên được coi là nhãn "bạc" có độ chính xác rất cao, không phải nhãn vàng.

Phát hiện phụ cho M3 (làm sạch): title phía Semantic Scholar còn mã MathML (`<mml:math …>`), LaTeX (`\documentclass[12pt]{minimal}…`) và title giả (`OUP accepted manuscript`). `title_clean` chưa xử lý các trường hợp này, nên các cặp đó có độ giống title thấp dù khớp.

**Độ giống title của R4** (`--sim`):
- Mặc định `chars`: **Jaccard các 3-gram ký tự của title đã bỏ khoảng trắng**. Nhờ bỏ khoảng trắng, `M 2 diffuser` và `M2Diffuser` cho cùng một tập 3-gram; một từ bị đổi chỉ làm lệch vài 3-gram.
- Bản đầu là `words`: tỉ lệ difflib trên các từ đã sắp xếp, ngưỡng 0,93. Cách này bỏ sót `M 2 diffuser` (0,89), vì khi sắp xếp, các mảnh "m" và "2" bị đẩy đi chỗ khác.

So hai cách trên các cặp thật:

| Cặp | words | chars |
|---|---|---|
| `M 2 diffuser` ↔ `M2Diffuser` (cùng bài) | 0,89 | 1,00 |
| `Towards` ↔ `Toward Generalist…` (cùng bài) | 0,99 | 0,94 |
| `Embodied SAM` ↔ `EmbodiedSAM` (cùng bài) | 0,93 | 1,00 |
| `DDPM` ↔ `Improved DDPM` (khác bài) | 0,90 | 0,83 |
| `Qwen3-TTS` ↔ `Qwen3-ASR` (khác bài) | 0,85 | 0,62 |
| `V2V4Real` ↔ `V2X-Real` (khác bài) | 0,81 | 0,60 |

`chars` tách hai nhóm (cùng bài / khác bài) rõ hơn. Ngoài ra, cặp nào lọt qua ngưỡng vẫn còn phải qua điều kiện cách biệt với ứng viên thứ hai, tác giả trùng và năm gần.

Số nhãn R4 trên pool 2.500 bài theo ngưỡng (`words` 0,93 cho 601):

| ngưỡng `chars` | 0,90 | **0,85** | 0,80 | 0,75 |
|---|---|---|---|---|
| nhãn R4 | 541 | **673** | 739 | 799 |

Chọn 0,85. Xem bằng mắt 12 cặp mới (có ở 0,85, không có ở `words` 0,93) thì cả 12 đều đúng: `blue holonomic…` ↔ `holonomic…`, `deepfake` ↔ `deep fake`, `RingAttention` ↔ `Ring Attention`, `P 5` ↔ `P5`, title có năm dính ở cuối… Ở 0,75 bắt đầu xuất hiện các cặp chỉ khớp một phần title, ví dụ title thiếu tiền tố tên hệ thống (`Grid-Agent: …`) hoặc bản đổi tên.

**Đo trên 168 nhãn tay của khoảng Bao (5 bài):** precision 1,000, recall 1,000 (168/168).
- R1: 31, R2: 55, R3: 79, R4: 3; không có nhãn sai.
- Với `words` 0,93 thì chỉ được 167/168 (bỏ sót `M 2 diffuser`).

Lưu ý quan trọng: R3 (cho phép năm lệch khi tác giả trùng), cùng với độ đo và ngưỡng của R4, đều được chỉnh sau khi xem chính 168 nhãn này. Lúc đầu R3 đòi năm lệch ≤ 1 và bỏ sót 5 bản tạp chí đăng sau bản preprint 2–3 năm. Vì vậy con số trên **lạc quan**. Số đo khách quan là đo trên nhãn tay của 4 khoảng còn lại, những nhãn chưa dùng để chỉnh luật.

Kiểm tra bằng mắt 15 cặp R4 ngẫu nhiên: cả 15 cặp đều đúng. Các dạng gặp: thừa chữ `blue` (lệnh màu LaTeX còn sót lại), `Embodied SAM` ↔ `EmbodiedSAM`, `^4He` ↔ `4 He`, title thiếu tiền tố `REALM:`.

## Bước 4: gộp nhãn (`merge_labels.py`) và cách dùng nhãn

```bash
# gộp mọi labels_manual_*.csv + labels_auto.csv -> WORK/labels.csv
python src/m3/merge_labels.py --work data/m3_all --suspects data/m3_all/suspect_auto_labels.csv
# khi các thành viên còn đang gán: các mức tối thiểu chỉ in cảnh báo
python src/m3/merge_labels.py --work data/m3_all --suspects data/m3_all/suspect_auto_labels.csv --allow-incomplete

# chia tập (script của Member 1); --per-range: mỗi thành viên góp 1 bài tay + 1 bài auto vào test và vào valid
python src/m3/split.py --labels data/m3_all/labels.csv --seed 11 --per-range \
    --stats data/m3_all/split_rankable.csv --out data/m3_all/splits.json

python src/tests/m3/test_merge_labels.py
```

**File ra**

| File | Nội dung |
|---|---|
| `labels.csv` | mọi entry đã được quyết định (nhãn tay và nhãn auto), các cột như file nhãn tay cộng thêm `range`. Giữ cả dòng `is_match = false`, vì bảng `predictions` có một dòng cho **mỗi** entry của các bài dùng cho matching (dòng không có reference thì `groundtruth_arxiv_id = null`). |
| `label_summary.csv` | theo khoảng: số bài, số entry, số nhãn tay và nhãn auto, số bài auto cần có |
| `split_rankable.csv` | `arxiv_id, n` dùng cho `split.py --stats`. Các bài **không được** vào test/valid có `n = 0`: bài không có reference, bài auto không ra nhãn nào, bài có nhãn auto đã biết là sai (`--suspects`) |

**Kiểm tra (lỗi thì dừng, không ghi file)**
- cột đúng; `source` là `manual` hoặc `auto`; `is_match` khớp với `label_ref_arxiv_id`;
- entry có trong `entries_clean.parquet` và chỉ xuất hiện một lần;
- nhãn là reference có arXiv ID của **chính bài đó**;
- không bài nào vừa tay vừa auto; không bài trong `manual_pick.csv` lọt vào nhãn auto;
- theo từng khoảng: ≥ 5 bài tay, mỗi bài ≥ 20 nhãn, ≥ 100 nhãn tay, và số bài auto ≥ 10% số bài ngoài phần gán tay. Với `--allow-incomplete`, các mức này chỉ in cảnh báo.

`data/m3_all/suspect_auto_labels.csv` liệt kê 13 nhãn auto đã biết là sai hoặc đáng ngờ (xem "Kiểm tra cuối" ở bước 3). Các nhãn này vẫn nằm trong train, vì 0,02% nhiễu không đáng kể khi train. Nhưng bài chứa chúng không được rút vào test/valid, để MRR không bị chấm theo đáp án sai.

**Kết quả chính thức** (đủ nhãn tay của 5 thành viên, chế độ chuẩn, đạt mọi kiểm tra): 2.525 bài, 126.065 entry, **63.612 nhãn**.

| Khoảng | Bài tay / nhãn tay | Bài auto / nhãn auto |
|---|---|---|
| Tram | 5 / 241 | 500 / 12.752 |
| Thu | 5 / 179 | 500 / 12.758 |
| Phat | 5 / 207 | 500 / 12.407 |
| Bao | 5 / 168 | 500 / 12.344 |
| Quan | 5 / 216 | 500 / 12.340 |
| **Tổng** | **25 / 1.011** | **2.500 / 62.601** |

Ngoài ra có 62.453 entry "không khớp" (598 ở bài tay, 61.855 ở bài auto): giữ lại cho bảng predictions, nhưng không phải sample. 19 bài bị giữ ngoài test/valid: bài không có nhãn nào, hoặc bài chứa nhãn auto nghi sai.

**Kiểm tra nhãn tay của 4 thành viên** (ngoài các kiểm tra của `import`):
- So với hint của luật: chỉ có 1 dòng không theo hint, và người gán đúng (`PhysRevC.90.057602`: S2 gắn nhầm DOI của bài 2014 cho bài 2021).
- 11 cặp khớp không có hint: đọc lại, tất cả đều hợp lý.
- 8 ca một reference được gán cho 2 entry: đều là cùng một bài được trích hai lần, có note giải thích.
- Quét các nhãn có title trùng nhưng tác giả mâu thuẫn: tìm ra **1 nhãn sai** và đã sửa. `NielsenChuang` (sách Nielsen & Chuang, không có trên arXiv) bị gán vào `1210.0736`, một bài khác trùng tên của Ya-Zhen Wang (2012).

**Đo khách quan luật auto** trên nhãn tay của 4 khoảng chưa từng dùng để chỉnh luật: precision **0,999**, recall **0,986** (832 đúng, 1 sai, 12 bỏ sót). Ca sai duy nhất là DOI mà S2 gắn nhầm. Các ca bỏ sót chủ yếu là title ≤ 2 từ (R3 đòi ≥ 3 từ), bài đổi tên, và tên phiên âm (Schütz/Schuetz). Chi tiết ở `auto_vs_manual_4ranges.csv`.

**Chia tập** (`--per-range`, seed 11): mỗi khoảng góp đúng 1 bài tay + 1 bài auto vào test, và 1 + 1 vào valid.

| Tập | Bài tay | Bài auto | Nhãn | Entry |
|---|---|---|---|---|
| train | 15 | 2.490 | 62.866 | 124.867 |
| valid | 5 | 5 | 400 | 598 |
| test | 5 | 5 | 346 | 600 |

Ba tập không trùng bài nào; bài trong test/valid đều có ứng viên và không chứa nhãn auto nghi sai.

### Cách dùng nhãn cho đặc trưng, train và đánh giá (Member 3, 4)

Theo giải đáp của thầy, **một nhãn là một cặp (entry, reference) khớp**. Entry có `is_match = false` **không phải là sample**.

- **Sample** = mỗi entry có `is_match = true`. Ứng viên của nó là **n reference của chính bài đó** (các dòng trong `candidates.parquet`):
  - 1 cặp **yes**: `(entry, label_ref_arxiv_id)`;
  - n − 1 cặp **no**: các reference còn lại của bài.
- **Không** tạo cặp "no" từ các entry `is_match = false`. Luật auto ưu tiên precision nên cố ý bỏ sót một số cặp khớp thật. Nếu coi các entry đó là toàn "no" thì sẽ dạy sai model.
- **MRR@5:** |Q| = số entry `is_match = true` của tập test. Ứng viên lấy từ references của chính bài đó. Bài có dưới 5 ứng viên thì các ô `cand_k` còn thiếu để null.
- **Bảng `predictions`:** một dòng cho mọi entry trong `labels.csv` (cả `true` lẫn `false`). Cột `partition` lấy từ `splits.json`; `groundtruth_arxiv_id` = `label_ref_arxiv_id` (để trống nếu `false`).
- **Chia tập** theo bài (`splits.json`); một bài không bao giờ vừa ở train vừa ở test.
- **Bài auto trong test được chấm theo nhãn do luật tạo ra.** Nếu đặc trưng của model gồm đúng các tín hiệu của luật (trùng ID, trùng DOI, title giống hệt) thì MRR trên phần auto sẽ cao gần như hiển nhiên. Vì vậy nên báo MRR **riêng** cho phần tay và phần auto, và báo thêm MRR khi che các đặc trưng link (xem phần "học đường tắt").
