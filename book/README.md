# arXiv Project — Nhóm 11: Jupyter Book

Báo cáo của đồ án, viết bằng [Jupyter Book 2](https://jupyterbook.org) (MyST). Mã nguồn crawler ở
[group11-arxiv-project](https://github.com/mituleefam/group11-arxiv-project).

```text
myst.yml                    cấu hình sách và mục lục (Milestone 1, sau này thêm Milestone 2)
index.md                    trang chủ
notebooks/                  các chương; 01 và 07 là Markdown, 02–06 là notebook
notebooks/report_utils.py   hàm dùng chung: đọc data/m1/, định dạng số, vẽ plotly
data/m1/                    số liệu tổng hợp do src/export_report_data.py của repo code tạo ra
.github/workflows/deploy.yml  build (có thực thi notebook) và đưa lên GitHub Pages
```

## Build trên máy

```bash
python -m venv .venv_book
.venv_book\Scripts\activate            # macOS/Linux: source .venv_book/bin/activate
pip install -r requirements.txt
jupyter book start --execute           # xem thử ở http://localhost:3000
jupyter book build --html --execute    # tạo _build/html
```

Cần Node.js (Jupyter Book 2 tự hỏi cài nếu chưa có). Phải kích hoạt venv trước để lệnh `jupyter` có trên PATH,
nếu không `--execute` báo *Could not load Jupyter session manager*.

## Cập nhật số liệu

Chạy `src/export_report_data.py` của repo code với thư mục dữ liệu của cả 5 thành viên, `--out` trỏ vào
`data/m1/` của repo này, rồi commit `data/m1/`. Xem `data/m1/README.md`.
