# Milestone 2 — Tổng hợp sửa lỗi và kiểm tra trước khi chạy toàn corpus

**Baseline:** ZIP `group11-arxiv-project-m2-main.zip`, commit được ZIP ghi nhận `03c8a6b5465f37547d8f9639148b34cf1b71a324` (07/11/2026). Các thay đổi ở đây không được đối chiếu với commit mới hơn trên GitHub. Các số liệu 5.000 bài/7.093 phiên bản và số lỗi trong `VIEC_CAN_SUA_TRUOC_KHI_CHAY_FULL_M2.md` là báo cáo do nhóm cung cấp; bản ZIP không chứa corpus GBao, nên **chưa xác minh trực tiếp những số liệu đó**.

## A. Sáu lỗi cần sửa và tình trạng trong source

| # | Vấn đề | File đã chỉnh | Cách sửa và phép kiểm tra |
|---|---|---|---|
| 1 | Câu/công thức/hình trùng trong **cùng version** dùng chung ID, làm node nhiều cha hoặc trùng cạnh | `dedup_elements.py`, `pipeline_m2.py`, `m2_contract.py`, `validate_m2.py`, `test_m2_member1.py`, `test_m2_member4.py` | Registry reset bộ đếm leaf mỗi version, khóa `(kind, content, thứ tự xuất hiện trong version)`; giữa version vẫn dùng chung ID theo thứ tự. Validator báo `FAIL` nếu nhiều cha hoặc cạnh trùng. Test lặp câu `OBDD.`. |
| 2 | `.bbl` bị phân loại thành `.bib` | `gatherer.py` | Bất kỳ file bibliography resolved đến `.bbl` được gọi `read_bbl`, không thêm vào `bib_files`; test `.bbl` duy nhất và cú pháp gọi tường minh. |
| 3 | `.bbl` dạng amsrefs/Harvard | `reference_parser/bbl_converter.py` | Nhận `\bib{key}{type}{...}` và chuyển `\harvarditem[...]{author}{year}{key}` sang `\bibitem{key}` trong vùng bibliography; test 2 định dạng. Phải kiểm lại các macro lạ trên corpus thật. |
| 4 | Gộp nhầm BibTeX vì title rác và citation key tái sử dụng | `reference_parser/dedup_bibtex.py` | Không so khớp theo `(ATLAS Collaboration)`, `accessed...`, chuỗi toàn số; phân biệt dấu số phiên bản GPT; cùng khóa **không đủ** để gộp 2 nguồn thiếu thông tin. Test ATLAS/GPT. Trường hợp cùng khóa khác tài liệu cần review trích dẫn theo từng version; không tự map `\cite` toàn cục khi mơ hồ. |
| 5 | Chọn nhầm file root supplement/diff | `gatherer.py` | Ưu tiên filename gợi ý `main`, `paper`, `manuscript` và giảm ưu tiên `supp`, `latexdiff`, `cover`, `response`, `appendix`; test ba file có `\documentclass`. Heuristic không chứng minh đúng 100% trên TeX lạ. |
| 6 | Chưa có benchmark nhóm M2 | `group_benchmark_m2.py` (mới), `group_benchmark.py`, `test_m2_full_fixes.py` | `group_benchmark.py --m2`: tổng số lượng và thời gian của 5 lần forced full + thời gian export-only thực đo; RAM lấy **max**, dung lượng lấy bảng Parquet **đã gộp**; tính tỷ lệ từ số lượng tổng. Bắt buộc chỉ định số bài dự kiến và có log hoàn tất. |

## B. Các điểm chất lượng bổ sung

| Vấn đề | Xử lý | Giới hạn |
|---|---|---|
| Macro tạp chí thiên văn trong `.bib` | `bibtex_converter.py` mở rộng macro `\apj`, `\mnras`, `\aap`, ... trước `clean_field` | Chỉ macro có trong bảng `JOURNAL_MACROS` của parser; macro tác giả tự định nghĩa vẫn cần review |
| Năm từ mã arXiv / năm lịch sử | `to_year` cho phép từ năm 1600 đến năm hiện tại + 1 và tránh `1805.02656` | Trường `year` tự do vẫn có thể cần kiểm tra trên văn bản thiếu dấu câu |
| Ký tự hỏng/đọc mã hóa | `gatherer.read_text` thử CP1252 rồi fallback Latin-1; validator cảnh báo `U+FFFD` | **Không thể khôi phục chính xác chữ đã mất trong nguồn**; phải tra source gốc |
| `\texorpdfstring{LaTeX}{PDF}` lặp heading | `cleaner.py` giữ đối số LaTeX đầu (hoặc PDF nếu đầu rỗng) | Những macro có ngữ nghĩa khác nằm ngoài phạm vi xử lý |
| `et al.` kết thúc câu | `sentence_splitter.py` xử lý một số từ mở đầu câu mới rõ ràng | Là heuristic; có thể sai trong câu phức tạp, cần đo bằng dữ liệu gán nhãn |
| Vài bài chậm 35–80 giây | **Chưa xác định chính xác nguyên nhân** (không có 5 source GBao trong ZIP) | Không tuyên bố đã tối ưu regex; chạy profile thực tế trước khi sửa |
| Pipeline có bài failed nhưng process trả về 0 | `pipeline_m2.py` trả mã 1 khi `parse_manifest` có bài chưa `done` | Vẫn phải kiểm tra validator, không chỉ exit code |

## C. Cách chạy lại sau khi merge

Thực thi từ thư mục root của repo, Python đã cài `src/requirements.txt`. Trên Windows PowerShell có thể dùng `$PY = '.\.venv\Scripts\python.exe'` và thay `python` bằng `& $PY`.

**1. Test offline (không cần corpus, không gọi mạng):**

```bash
python -m unittest discover -s src -p 'test*.py' -v
```

Trong môi trường xây dựng patch: **184 test được phát hiện: 183 test chạy đạt, 1 test Parquet bị skip** do không thể cài `pyarrow` vì môi trường không kết nối mạng. **Phải chạy lại toàn bộ trên máy/Colab đã cài pyarrow.**

**2. Kiểm tra 5–10 bài đại diện của GBao/Tram:**

```bash
python src/pipeline_m2.py --data-dir DATA_DIR --limit 10 --workers 2 --force
python src/validate_m2.py --data-dir DATA_DIR --limit 10 --require-parquet
```

Chú ý `--limit` có thể khiến bảng Parquet chỉ xuất 10 bài, còn thư mục `parsed/` của các bài khác là cũ. **Không dùng bảng đó trong báo cáo full.** Lựa chọn test phải có: nhiều version, nhiều `\input`, `.bbl`, tham khảo dễ gộp sai, PDF-only. Kiểm tra lại đúng mã được báo trong tài liệu GBao; những mã `2604.*` thuộc GBao không có trong ZIP này.

**3. Chạy full trên từng khoảng riêng:**

- Copy corpus của từng thành viên từ Drive xuống local disk `/content` của Colab CPU-only; không chạy trực tiếp trên Drive. Giữ nguyên thư mục M1 gốc.
- Sau sửa Registry và BibTeX **bắt buộc tái parse** bằng `--force`, không dùng `--keep`, không dùng kết quả Parquet cũ.

```bash
python src/benchmark_m2.py --data-dir DATA_DIR --workers 2 --force \
  --environment google_colab --cpu-only-declared
python src/validate_m2.py --data-dir DATA_DIR --require-parquet
python src/reference_parser/reference_stats.py --data-dir DATA_DIR
python src/statistics_m2.py --data-dir DATA_DIR
```

Thực hiện riêng cho 5 thư mục `DIR_1` đến `DIR_5`. Giữ nguyên `logs/performance_log_m2.csv`, `logs/performance_summary_*.json`, `logs/validation_report_m2.csv`, `logs/summary_statistics_m2.csv`. Không xóa log khi chưa sao lưu. **Lệnh fetch_bbl.py** chỉ chạy khi corpus M1 còn thiếu `.bbl` và được phép tải bù; bước tải bổ sung diễn ra **trước** benchmark, tránh làm lẫn runtime network với parser.

**4. Đo lần xuất Parquet gộp cả nhóm:**

```bash
mkdir -p /content/export_workspace /content/export_logs
python src/benchmark.py --data-dir /content/export_workspace \
  --output-dir /content/export_logs --interval 0.5 -- \
  python src/pipeline_m2.py --data-dir DIR_1 DIR_2 DIR_3 DIR_4 DIR_5 \
  --export-only --out /content/merged_tables
```

Giữ đúng `performance_summary_*.json` của **lần export này**, không sử dụng một summary crawl M1 hoặc một lần parse thông thường.

**5. Gộp benchmark cho nhóm:**

```bash
python src/group_benchmark.py --m2 \
  Tram=DIR_1 Quan=DIR_2 Bao=DIR_3 M4=DIR_4 M5=DIR_5 \
  --expected-per-member 5000 \
  --tables-dir /content/merged_tables \
  --export-summary /content/export_logs/performance_summary_EXPORT_RUN.json \
  --out /content/group_m2_report
```

Thay các tên member, đường dẫn và `EXPORT_RUN` bằng log thật; `5000` phải khớp bảng phân công chính thức của nhóm. Nếu một khoảng chỉ được giao số bài khác, không dùng giá trị này; cần điều chỉnh kiểm tra hoặc dùng một lần tổng hợp với dữ liệu số bài đúng từng khoảng. Script chỉ xuất **complete** khi mỗi thành viên có benchmark forced full thành công, số bài khớp, thống kê mới hơn benchmark, số FAIL validation bằng 0, và có Parquet gộp + summary export thành công. `WARN` vẫn cần ghi phân tích, không xem như PASS tuyệt đối.

## D. Review trước merge và trước báo cáo

- Kiểm tra `git diff` và chỉ merge vào branch code chung sau khi chạy regression; đừng ghi đè commit mới hơn mà không diff.
- Kiểm tra những ID GBao được nêu trong file lỗi gốc. Riêng vấn đề title mơ hồ, review thủ công `citation_key`, `title`, `doi`, `year`, `authors` và rename map qua từng version.
- Kiểm tra `validation_report_m2.csv` không có `FAIL`, `unique_parent_per_version`, `duplicate_hierarchy_edges` đều `PASS`, bản PDF-only được ghi nhận, không còn stand-in.
- Đối chiếu bảng `elements`/`hierarchy`/`bibtex` sau khi full export với tổng JSON bằng validator. Điều này xác nhận **schema và số dòng**, chưa phải so khớp toàn bộ từng giá trị nội dung. Muốn tuyên bố JSON–Parquet giống 100% cần phép so sánh từng bản ghi riêng.
- Các chỉ số tóm tắt từ `VIEC_CAN_SUA_TRUOC_KHI_CHAY_FULL_M2.md` là kết quả **trước sửa**; không được tái sử dụng làm kết quả sau sửa. Chỉ dùng số đo Colab CPU-only sau khi tất cả bug được fix và parser chạy full lại.
- Bộ source ZIP **không bao gồm corpus hoặc log thực tế** để xác minh lại 5.000 bài của GBao, 5.000 bài của Tram và kết quả tất cả thành viên. Đó là điều kiện nghiệm thu còn thiếu, không phải test đã qua.
