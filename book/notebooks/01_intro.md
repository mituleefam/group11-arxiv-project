---
title: "Chương 1: Giới thiệu"
short_title: "1. Giới thiệu"
---

> **Môn học:** Nhập môn Khoa học Dữ liệu (Introduction to Data Science)
> **Đồ án:** arXiv Project
> **Nhóm thực hiện:** Nhóm 11
> **Giai đoạn:** Milestone 1 — Thu thập dữ liệu (Data Crawling)

## 1. Thành viên và phân công

Nhóm gồm **5 thành viên**. Mỗi người chủ trì một mô-đun của pipeline chung. Khoảng arXiv ID được giao của cả 5 thành viên được **crawl lần lượt trong một lượt chạy** bằng cùng một bộ code trên Google Colab; dữ liệu của mỗi khoảng vẫn nằm trong một thư mục riêng.

| STT | Họ và tên | MSSV | Vai trò | Mô-đun phụ trách |
| :---: | :--- | :---: | :--- | :--- |
| 1 | `[Họ và tên]` | `[MSSV]` | Member 1 — Entry Discovery & Metadata | `discovery.py`, `metadata.py`, `main.py` |
| 2 | `[Họ và tên]` | `[MSSV]` | Member 2 — Source Downloader & Extraction | `downloader.py`, `extractor.py`, `checkpoint.py`, `rate_limiter.py` |
| 3 | `[Họ và tên]` | `[MSSV]` | Member 3 — References Crawling | `references.py`, `ref_dates_kaggle.py` |
| 4 | `[Họ và tên]` | `[MSSV]` | Member 4 — Data Validation & Performance | `validate.py`, `corpus_statistics.py`, `benchmark.py`, `group_benchmark.py` |
| 5 | `[Họ và tên]` | `[MSSV]` | Member 5 — Jupyter Book & Documentation | Repo Jupyter Book, `README.md` |

Khoảng ID thực tế của từng thành viên (ID đầu, ID cuối, số ID) được đọc từ dữ liệu crawl và trình bày tự động ở [Chương 3](./03_data_organization.ipynb).

## 2. Bối cảnh

[arXiv](https://arxiv.org/) là kho lưu trữ mở với hơn hai triệu bài báo khoa học. Phần lớn các bài tập crawl arXiv chỉ thu thập metadata. Đồ án này thu thập **mã nguồn LaTeX đầy đủ** của bài báo thay cho file PDF, vì:

1. **Giữ được cấu trúc phân cấp:** chương, mục, danh sách, bảng biểu được khai báo tường minh trong LaTeX, còn PDF chỉ còn các khối chữ đã dàn trang.
2. **Giữ nguyên công thức toán:** công thức nằm ở dạng mã LaTeX gốc, không bị biến dạng như khi trích xuất chữ từ PDF.
3. **Liên kết trích dẫn rõ ràng:** lệnh `\cite{}` trỏ trực tiếp đến khóa trong `\bibitem` hoặc file `.bib`.

Theo tài liệu của arXiv, khoảng **97%** bài nộp có mã nguồn TeX, phần còn lại chỉ có PDF. Với những phiên bản không có TeX, nhóm vẫn giữ một bản ghi rỗng thay vì bỏ bài báo, đúng yêu cầu của đề.

Hình ảnh chiếm phần lớn dung lượng của gói nguồn nhưng không cần cho các milestone sau. Vì vậy crawler **chỉ trích xuất các file `.tex` và `.bib`** từ gói nguồn: file hình không bao giờ được ghi vào corpus, còn các lệnh `\begin{figure}`, `\includegraphics` trong file `.tex` được giữ nguyên từng byte. Tỷ lệ dung lượng giảm được nhờ cách làm này được đo trên dữ liệu thật ở [Chương 4](./04_crawling_results.ipynb).

## 3. Mục tiêu của Milestone 1

1. **Entry discovery:** phân giải khoảng arXiv ID được giao qua arXiv API (`id_list`), phân biệt ID tồn tại, ID không tồn tại và lỗi API, đồng thời xác định danh sách phiên bản `v1, v2, …` của mỗi bài.
2. **Metadata:** lấy tiêu đề, tác giả, ngày nộp, các ngày sửa đổi và nơi công bố của từng bài.
3. **Mã nguồn LaTeX:** tải riêng từng phiên bản từ `https://export.arxiv.org/src/<id>v<N>`, nhận diện định dạng thật của file tải về và chỉ giữ `.tex`/`.bib` với nguyên cấu trúc thư mục con.
4. **References:** thu thập danh sách tài liệu tham khảo của mỗi bài qua Semantic Scholar Graph API, giữ toàn bộ reference cục bộ và đánh dấu các reference có arXiv ID.
5. **Kiểm tra và đo hiệu năng:** kiểm tra tự động tính đầy đủ của corpus và đo thời gian chạy, RAM, dung lượng đĩa trên Google Colab CPU-only.
6. **Báo cáo:** mọi bảng và biểu đồ trong sách được sinh bằng code khi build Jupyter Book.

## 4. Lộ trình đồ án

```{mermaid}
flowchart LR
    A["Milestone 1<br><b>Data Crawling</b><br>TeX/Bib, metadata, references"] --> B["Milestone 2<br><b>Parsing & Standardization</b><br>cây phân cấp, BibTeX, khử trùng lặp"]
    B --> C["Milestone 3<br><b>Reference Matching</b><br>pipeline ML, MRR@5, Hugging Face"]
```

| Milestone | Mục tiêu chính | Sản phẩm |
| :---: | :--- | :--- |
| **M1** (chương này) | Thu thập mã nguồn LaTeX, metadata và references của mọi phiên bản | Corpus làm việc của nhóm, báo cáo M1 |
| **M2** | Dựng cây phân cấp, chuẩn hóa nội dung, chuyển `\bibitem` sang BibTeX, khử trùng lặp | Bảng `elements`, `hierarchy`, `bibtex`, báo cáo M2 |
| **M3** | Ghép BibTeX entry với reference có arXiv ID, đánh giá bằng MRR@5 | Dataset trên Hugging Face, poster A2 |

Milestone 1 không nộp dữ liệu: corpus được nhóm giữ lại làm đầu vào cho Milestone 2 và 3.

## 5. Cấu trúc phần báo cáo Milestone 1

- [Chương 2](./02_methodology.ipynb): cách hoạt động của crawler, công cụ và thư viện đã dùng.
- [Chương 3](./03_data_organization.ipynb): tổ chức corpus và kết quả discovery.
- [Chương 4](./04_crawling_results.ipynb): các số liệu thống kê đề bài yêu cầu.
- [Chương 5](./05_performance.ipynb): thời gian chạy, RAM và dung lượng đĩa.
- [Chương 6](./06_errors_limitations.ipynb): kết quả kiểm tra dữ liệu, các lỗi còn lại và hạn chế.
- [Chương 7](./07_conclusion.md): tổng kết.

Mã nguồn: [group11-arxiv-project](https://github.com/mituleefam/group11-arxiv-project).
