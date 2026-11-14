---
title: "Chương 7: Tổng kết Milestone 1"
short_title: "7. Tổng kết"
---

## 1. Kết quả đạt được

Nhóm đã xây dựng một pipeline crawl chạy lại được, mỗi thành viên dùng cùng một bộ code cho khoảng arXiv ID được giao của mình:

- **Discovery và metadata** qua arXiv API: phân giải mọi ID được giao, xác định danh sách phiên bản của từng bài, lưu metadata theo đúng các cột của bảng `papers` (tiêu đề, tác giả, ngày nộp, các ngày sửa đổi, nơi công bố).
- **Mã nguồn LaTeX của mọi phiên bản** từ `export.arxiv.org/src/<id>v<N>`: nhận diện định dạng thật của file, chỉ trích xuất `.tex`/`.bib` với nguyên đường dẫn tương đối, ghi bản ghi rỗng cho phiên bản PDF-only hoặc không có TeX.
- **References** qua Semantic Scholar Graph API: giữ toàn bộ reference trong `references_all.json` và tách các reference có arXiv ID ra `references.json`, sẵn sàng chuyển thành bảng `references` ở Milestone 3.
- **Độ tin cậy:** tuân thủ giới hạn request của arXiv và Semantic Scholar, retry theo lượt, checkpoint từng phiên bản, chạy tiếp được sau khi Colab bị ngắt mà không tải lại phần đã xong.
- **Kiểm tra và đo đạc:** `validate.py` kiểm tra toàn bộ corpus, `corpus_statistics.py` tính các chỉ số, `benchmark.py` đo thời gian, RAM, dung lượng đĩa trên Colab CPU-only.

Các con số cụ thể (số bài crawl thành công, tỷ lệ thành công, kích thước trước/sau khi bỏ hình, số reference trung bình, thời gian, RAM, dung lượng) được tính khi build sách ở [Chương 4](./04_crawling_results.ipynb) và [Chương 5](./05_performance.ipynb).

## 2. Hạn chế còn lại

Danh sách đầy đủ và số lượng từng loại có ở [Chương 6](./06_errors_limitations.ipynb). Tóm tắt:

- Một số phiên bản arXiv trả HTTP 404 hoặc chỉ có PDF, nên không có mã nguồn. Các phiên bản này được ghi nhận, không bị bỏ qua.
- Semantic Scholar chưa lập chỉ mục một số bài mới, hoặc có danh sách reference nhưng không chia sẻ. Ngày nộp arXiv của reference không phải lúc nào cũng có.
- Các khoảng ID được crawl lần lượt trên một runtime Colab; do giới hạn 12 giờ, lượt chạy trải qua nhiều phiên và tiếp tục từ bản lưu, nên thời gian chạy được báo cáo theo từng bước (invocation) và cộng lại, không phải một lần chạy liền mạch duy nhất.

## 3. Chuẩn bị cho Milestone 2

Corpus Milestone 1 là đầu vào trực tiếp của Milestone 2:

- Cấu trúc thư mục con của mỗi phiên bản được giữ nguyên, nên các lệnh `\input`/`\include` vẫn phân giải được để tìm compilation path.
- File `.tex` không bị chỉnh sửa, giữ nguyên `\begin{figure}`, `\cite`, `\bibitem` để dựng cây phân cấp và trích xuất BibTeX.
- `references_all.json` và `references.json` là tập ứng viên để ghép với các BibTeX entry ở Milestone 3.
