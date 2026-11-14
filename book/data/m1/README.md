# data/m1 — số liệu tổng hợp của Milestone 1

Các notebook của phần Milestone 1 đọc dữ liệu từ thư mục này khi build sách. Thư mục chỉ chứa các bảng tổng hợp
(số lượng, kích thước, trạng thái, thời gian), không có nội dung LaTeX hay tiêu đề reference, nên được commit vào repo sách.

Tạo lại bằng script của repo code (chạy sau khi mọi thành viên đã crawl xong và đã chạy `validate.py --finished`
trên thư mục của mình; các chỉ số thống kê do script tự tính lại):

```bash
python src/export_report_data.py m1=<DATA_M1> m2=<DATA_M2> m3=<DATA_M3> m4=<DATA_M4> m5=<DATA_M5> --out <mst1-book>/data/m1
```

| File | Nội dung |
| :--- | :--- |
| `meta.json` | Thời điểm xuất, commit code, tên thành viên, cảnh báo |
| `members.csv` | Một dòng mỗi thành viên và dòng `group` (từ `group_benchmark.py`) |
| `statistics.csv` | Các chỉ số của `corpus_statistics.py`, theo thành viên |
| `papers.csv.gz` | Một dòng mỗi ID: discovery, metadata, references |
| `versions.csv.gz` | Một dòng mỗi phiên bản: trạng thái, định dạng, kích thước, thời gian |
| `run_log.csv` | Thời gian từng bước của từng lần chạy |
| `benchmark_runs.csv` | Từng lần chạy `benchmark.py` |
| `performance_samples.csv.gz` | Mẫu RAM/dung lượng đĩa theo thời gian |
| `validation_counts.csv`, `validation_issues.csv` | Kết quả `validate.py` |
