# Milestone 2 — Member 4: Deduplication, Validation & Benchmark

Phần này phụ trách **ID của các phần tử LaTeX sau chuẩn hóa**, kiểm tra tính hợp lệ dữ liệu đầu ra và tổng hợp kết quả xử lý/hiệu năng. Code nằm trong `src/`; không thay thế các module crawling, hierarchy hoặc reference parser của thành viên khác.

## Các file phụ trách

| File | Nội dung |
| --- | --- |
| `dedup_elements.py` | Registry cấp và tái sử dụng `element_id` trong phạm vi một bài báo, trên mọi version. |
| `validate_m2.py` | Kiểm tra JSON từng bài, cấu trúc cây, citation key và schema 3 bảng Parquet. |
| `statistics_m2.py` | Tổng hợp parsing coverage, số phần tử/trích dẫn trước–sau dedup và phân loại lỗi. |
| `benchmark_m2.py` | Gọi `benchmark.py` có sẵn của M1 để đo toàn pipeline M2; thêm log tổng hợp riêng. |
| `test_m2_member4.py` | Test Registry, validation, statistics và tích hợp bằng dữ liệu giả lập. |
| `benchmark_colab_m2.ipynb` | Chạy kiểm thử và benchmark trên Colab CPU-only với corpus M1 của nhóm. |

## Điểm tích hợp với pipeline

- `pipeline_m2.py` (Member 1) gọi `Registry(arxiv_id).element_id(kind, content, parent_id, occurrence)` khi dựng node và gọi `rows()` để xuất bảng `elements`. **Giữ nguyên interface trong `m2_contract.py`.**
- Registry dùng **exact match sau khi chuẩn hóa whitespace**, không lowercase, không fuzzy matching. `sentence` / `formula` / `figure` trùng `kind` + `content` sẽ dùng chung ID giữa các version. Heading/paragraph xét thêm `occurrence`; node cấu trúc xét thêm `parent_id` và `occurrence`. Root của từng version có nội dung riêng.
- Bảng `hierarchy` vẫn lưu các cạnh theo từng version: một `element_id` có thể xuất hiện ở nhiều cạnh, kể cả trong cùng version. Không dùng điều kiện `element_id` duy nhất trên bảng cạnh.
- Dữ liệu tham khảo do Member 3 xử lý **trước** bước dedup elements để các `\cite{...}` được thống nhất key.

## Kiểm tra và đầu ra

Chạy tại **thư mục gốc repository**, với `data/` là corpus Milestone 1 đã có sẵn (không nằm trong source repo):

```bash
python -m unittest discover -s src -p test_m2_member4.py -v
python src/pipeline_m2.py --data-dir data --limit 5 --workers 2 --force
python src/validate_m2.py --data-dir data --limit 5 --require-parquet
python src/benchmark_m2.py --data-dir data --limit 5 --workers 2 --force
python src/statistics_m2.py --data-dir data --limit 5
```

| Output | Ý nghĩa |
| --- | --- |
| `data/corpus/<paper>/parsed/{elements,hierarchy,bibtex}.json` | Kết quả của pipeline; không tạo thủ công. |
| `data/logs/validation_report_m2.csv` | Các kiểm tra `PASS` / `WARN` / `FAIL`, theo bài và version. |
| `data/logs/performance_log_m2.csv` | Wall time, RAM được lấy mẫu, dung lượng Parquet và cấu hình chạy. |
| `data/logs/summary_statistics_m2.csv` | Các metric đưa vào Jupyter Book. |

**Lưu ý khi review:** `--limit 5` chỉ phục vụ smoke test; Parquet có thể chứa kết quả các lần chạy trước, nên không dùng những số này làm thống kê nghiệm thu. Để đối chiếu đầy đủ số dòng JSON–Parquet, chạy pipeline/validator/statistics trên cùng **toàn bộ** corpus và kiểm soát dữ liệu đầu ra còn sót từ lần chạy trước. Benchmark chính thức phải chạy trên **Google Colab CPU-only**, không lấy số liệu máy cá nhân làm số liệu báo cáo.

## Các điểm cần review

1. **Registry:** Có tái sử dụng ID đúng qua version không? Node trùng nội dung nhưng khác `kind` có bị gộp nhầm không? Heading lặp và root theo version có phân biệt đúng không?
2. **Validation:** Kiểm tra đúng root, chu trình, orphan node, schema Arrow/Parquet, các phiên bản không có TeX và citation key chưa khớp. Citation thiếu được ghi `WARN`, không mặc định là lỗi parser.
3. **Statistics:** “Trước dedup” của elements là **số lần xuất hiện** (số cạnh + root), “sau” là **số element phân biệt**; tỷ lệ giảm số bản ghi **không phải** tỷ lệ giảm dung lượng file. BibTeX before-dedup chỉ có khi `reference_log.csv` từ Member 3 đầy đủ.
4. **Benchmark:** `benchmark_m2.py` tái sử dụng `benchmark.py` M1, không chỉnh file M1. RAM peak là **giá trị lớn nhất trong các mẫu đo**, không phải đỉnh tuyệt đối; số đo có thể tính trùng trang nhớ dùng chung giữa các process.
5. **Tính tương thích:** Không chỉnh sửa code Member 1–3 hoặc `corpus_statistics.py`. Khi module parsing/normalization thay đổi, cần chạy lại integration test và validation trước khi chốt số liệu.

Các file CSV/log và Parquet thuộc dữ liệu chạy, **không commit toàn bộ corpus lên GitHub**. Member 5 chỉ cần lấy bảng thống kê/log đã chọn để dựng biểu đồ Jupyter Book.
