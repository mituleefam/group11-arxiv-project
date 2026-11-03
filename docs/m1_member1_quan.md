# Milestone 1: phần việc của Quân (Member 1) và lý do

Tài liệu này giúp cả nhóm theo dõi những gì Quân đã thêm vào crawler Milestone 1:
mỗi phần làm gì, dùng kỹ thuật nào, và **vì sao**: đề yêu cầu (trích nguyên văn),
thầy trả lời trong sheet Q&A, tài liệu của arXiv / Semantic Scholar, hoặc số đo
từ log chạy thật của nhóm.

- Đề: <https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/crawling>
- Q&A của thầy: sheet "[IntroDS] QA Replies - 2627-1" (sheet *Milestone 1* và *Milestone 3*)
- Trên GitHub, commit của Quân mang tên `EugeneZ-netizen` hoặc `24127226 - PHẠM TRẦN ANH QUÂN`.

---

## 1. Tóm tắt nhanh

| Commit | Ngày | Nội dung chính |
| :-- | :-- | :-- |
| `3d40029` | 15/10 | `common.py`, `rate_limiter.py`: cấu trúc thư mục dữ liệu, manifest, log CSV, bộ giới hạn tốc độ dùng chung |
| `734950e` | 15/10 | `discovery.py`: từ khoảng ID được giao ra danh sách bài và số phiên bản |
| `2b834d3` | 15/10 | `metadata.py`, `main.py`: metadata từng phiên bản, pipeline chạy một lệnh |
| `614b86e` | 24/10 | HTTP 429: dừng **mọi** request arXiv một lúc thay vì làm hỏng cả chuỗi phiên bản |
| `2909759` | 25/10 | Hạn chờ rõ ràng khi arXiv từ chối, dừng ngay khi bấm Stop, mỗi thư mục dữ liệu chỉ một crawler |
| `7b9e8a0` | 25/10 | Tải lại phiên bản có file bị mất, dừng hẳn khi đầy ổ |
| `2d9dbc5` | 26/10 | Thử lại lỗi theo **lượt** (pass), chỉ dừng toàn bộ khi lỗi chung, `--no-arxiv-dates` |
| `562b051` | 26/10 | `ref_dates_kaggle.py` (ngày của bài được trích lấy từ Kaggle), `group_benchmark.py` |
| `a1582b5` | 27/10 | Tải song song theo nhịp bulk data của arXiv, 429 không cộng dồn, tuỳ chọn proxy |
| `ca0fe44`, `ae69170`, `101cbb5` | 27–28/10 | Notebook chạy trên Colab (thử, nhóm, bản final) |
| `1930137` | 28/10 | References: nhiều bài trong một request `/paper/batch` của Semantic Scholar |
| `1d8fa3b` | 29/10 | Crawl cả nhóm theo từng bước có lưu Drive (`chunked_crawl.py`, `snapshot.py`), gác file arXiv chưa trả |

Phần tải mã nguồn ban đầu (`downloader.py`, `extractor.py`, `checkpoint.py`) là của
Member 2, references ban đầu (`references.py`) của Member 3, kiểm tra và benchmark
(`validate.py`, `benchmark.py`, `corpus_statistics.py`) của Member 4. Quân sửa thêm
trên các file đó ở những commit ghi trong bảng.

---

## 2. Đề yêu cầu gì và file nào đáp ứng

| Đề yêu cầu (trích nguyên văn) | Ở đâu trong code |
| :-- | :-- |
| "resolve these assigned IDs and to locate every version of each paper" | `discovery.py` |
| "Each version is retrieved individually through the versioned /src/ endpoint" | `downloader.py` (`export.arxiv.org/src/<id>v<N>`) |
| "Point automated downloads at this mirror and never at the main site arxiv.org" | `rate_limiter.py`, `downloader.py`: chỉ gọi `export.arxiv.org` |
| "wait at least 3 seconds between successive calls and use one connection at a time" (API) | `rate_limiter.ARXIV`: 3 s, 1 kết nối |
| "for such versions you should keep an empty record in the corpus rather than dropping the publication" | `versions.json` ghi `pdf_only`, `no_tex`, `not_found` |
| "extract only the .tex and .bib files from each source archive" | `extractor.py` (Member 2), giữ nguyên |
| "The reference metadata must additionally be crawled for each publication via the Semantic Scholar API" | `references.py` (Member 3), Quân thêm `/paper/batch` |
| "Keep one working folder per publication, for example folder 2310-12345" | `common.DataLayout`: `corpus/<yymm-id>/` |
| "running time and memory footprint ... the maximum disk storage required" | `benchmark.py` (Member 4), `group_benchmark.py` (Quân) |
| "The testbed for the final benchmarking is a Google Colab instance running on CPU-only mode." | Notebook `src/chay-crawler-nhom-final.ipynb` |

---

## 3. Chi tiết từng phần

### 3.1. Nền tảng dùng chung (`common.py`)

- **`DataLayout`**: một chỗ duy nhất quy định đường dẫn (`work/`, `corpus/<yymm-id>/`, `logs/`).
  Mọi module dùng chung, không ai tự ghép đường dẫn.
- **`Manifest`** (`work/paper_manifest.jsonl`): mỗi dòng là trạng thái một bài, ghi
  thêm chứ không ghi đè. Dòng sau của cùng ID thắng dòng trước, nên chạy lại hay bị
  ngắt giữa chừng đều không hỏng file (dòng ghi dở bị bỏ qua khi đọc).
- **`atomic_write_json`**: ghi ra file tạm rồi đổi tên, nên máy tắt đột ngột không để
  lại file JSON ghi dở.
- **`CsvLog`**: mỗi dòng log được ghi xuống đĩa ngay, phục vụ báo cáo và benchmark.
- **`RunLock`** (`work/crawler.lock`): hai crawler không thể chạy chung một thư mục dữ liệu.

*Vì sao:* chạy lại an toàn (idempotent) là điều kiện để chia benchmark thành nhiều
phiên Colab. Thầy cho phép điều này nhưng yêu cầu giữ log: "các em được phép chia
thành nhiều phiên với thông tin log thời gian từng phiên cần được giữ lại để đối chiếu".

### 3.2. Tìm bài và phiên bản (`discovery.py`)

- Từ 4 cột trong sheet (`--range 2026-05 12338 2026-05 17337`), sinh ra các ID, kể cả
  khoảng đi qua nhiều tháng.
- Hỏi API arXiv **100 ID mỗi request** (`id_list`), thay vì từng ID một. Với 5000 ID
  chỉ cần khoảng 50 request, tức khoảng 2,5 phút theo nhịp 3 s.
- Lấy số phiên bản từ ID của bản mới nhất (`...v3` → `v1, v2, v3`).
- **Hỏi lại các ID bị thiếu** trong câu trả lời (`discovery_recheck`) trước khi kết
  luận `not_found`. API arXiv từng trả về feed rỗng mà vẫn mã 200 (đã được báo ở thư
  viện `arxiv.py`, issue #43, với truy vấn phân trang); nếu không hỏi lại thì một bài
  có thật mà lỡ vắng trong câu trả lời sẽ bị ghi là không tồn tại.
- Phân biệt `not_found` (arXiv không có) và `api_error` (API lỗi, lần chạy sau làm lại).

### 3.3. Metadata (`metadata.py`)

- `metadata.json` của mỗi bài: tiêu đề, tác giả, ngày nộp, các ngày sửa, venue,
  chuyên mục, và **metadata của từng phiên bản**, vì đề yêu cầu
  "for each version, its paper metadata".
- Bản mới nhất lấy lại từ câu trả lời của discovery (không hỏi lần hai); các bản cũ
  hỏi bằng ID có phiên bản, 100 ID mỗi request. Bài chỉ có `v1` không tốn request nào.

### 3.4. Bộ giới hạn tốc độ và xử lý khi arXiv từ chối (`rate_limiter.py`)

Mọi request tới arXiv đi qua một bộ giới hạn **dùng chung cho mọi luồng**, nên
không module nào tự `sleep` riêng:

- **`ARXIV`** (API): 3 s giữa hai request, một kết nối, đúng câu của đề ở bảng mục 2.
- **`ARXIV_EXPORT`** (tải mã nguồn): theo trang bulk data của arXiv,
  "bursts at 4 requests per second with a 1 second sleep, per burst"
  (<https://info.arxiv.org/help/bulk_data.html>), tức trung bình 2 request/giây,
  nhiều file tải cùng lúc. API cũng tính vào nhịp này, nên tổng request không vượt.
  Xem mục 4.1 về chỗ này khác câu chữ của đề.
- **HTTP 429**: nhân viên arXiv giải thích 429 "relates to server capacity and total
  number of users, not to individual usage" và "the only fix is try again later". Vì
  vậy khi gặp 429, **mọi** request arXiv dừng chung một khoảng, tăng gấp đôi mỗi lần
  bị từ chối tiếp, có trần. Các 429 về cùng lúc trên nhiều kết nối được tính là một
  lần (không cộng dồn).
- **HTTP 503 hay mất kết nối ở một file**: chỉ file đó được thử lại, các file khác
  tải tiếp. Chỉ khi hai file *khác nhau* cùng lỗi trong 2 phút mới coi là lỗi chung.
- **File arXiv không trả được trong nhiều giờ** (gặp thật trong lần chạy tháng 10:
  vài phiên bản trả 503/429 hàng giờ liền, mỗi lần làm cả crawler dừng, tổng khoảng
  30 phút): lỗi 2 lần thì **gác lại** để lượt sau tải, không làm dừng file khác.
- **Bỏ cuộc có hạn**: arXiv từ chối liên tục 30 phút (`--max-wait-minutes`) thì dừng
  với mã 3, mọi thứ đã xong vẫn được lưu.
- Mọi lần dừng ghi vào `logs/arxiv_throttle_log.csv` để báo cáo.

*Vì sao:* thầy chấm cách xử lý lỗi ("Thầy sẽ có chấm phương pháp xử lý các case error
của nhóm trong quá trình triển khai cào nhé") và thời gian chờ tính vào runtime ("quá
trình retry và delay này có tính vào tổng runtime luôn nên các em cũng cần nghĩ cách
để giảm thời gian chờ này").

### 3.5. Pipeline một lệnh và thử lại theo lượt (`main.py`)

- `python src/main.py --range ... --data-dir ...` chạy discovery → metadata →
  (tải mã nguồn **song song với** references). Hai việc sau gọi hai server khác nhau
  (arXiv và Semantic Scholar), mỗi bên có giới hạn riêng, nên chạy cùng lúc không
  vi phạm giới hạn nào mà tiết kiệm thời gian.
- **Thử lại theo lượt**: lỗi tạm thời không được thử lại ngay tại chỗ mất nhiều phút;
  lượt chạy làm tiếp các việc khác, hết lượt mới chạy lượt thử lại cho riêng phần còn
  thiếu. `logs/pass_log.csv` ghi lại mỗi lượt còn thiếu gì, để báo cáo tỉ lệ thành
  công theo từng lượt. Thầy cho phép: "các nhóm được phép cào nhiều lần với số lần và
  tỉ lệ thành công mà nhóm quyết định".
- Kết quả cuối cùng (404 của bản đã rút, bản chỉ có PDF) không bao giờ thử lại.
- `--limit N` để chạy thử, `--stages` để chạy riêng từng bước, bấm Stop là dừng ngay
  và lần sau làm tiếp.

### 3.6. Tải mã nguồn (sửa trên `downloader.py`, `checkpoint.py`, `extractor.py` của Member 2)

- Tải **nhiều bài cùng lúc**, mỗi bài một luồng, mọi luồng chung bộ giới hạn ở mục 3.4.
  Một file thường mất vài giây mới về hết; tải từng file một thì không bao giờ đạt
  được nhịp cho phép.
- Bài từng lỗi được xếp **tải sau cùng**; file bị gác không bị tính là "lỗi liên tiếp".
- Phiên bản đã ghi là xong nhưng file trên đĩa bị mất thì **tải lại**; đĩa đầy thì
  **dừng hẳn** thay vì ghi hỏng hàng loạt.

### 3.7. References: nhiều bài trong một request (sửa `references.py` của Member 3)

- Dùng `POST /paper/batch` của Semantic Scholar để lấy references của **nhiều bài
  trong một request**, thay vì từng bài, từng trang.
- Tài liệu Semantic Scholar ghi endpoint này "Can only return up to 9999 citations at
  a time", và vượt mức đó thì không báo lỗi mà trả thiếu. Vì vậy mỗi request chỉ hỏi
  tối đa 8000 references, và bài nào trả về thiếu so với `referenceCount` thì lấy lại
  theo từng trang.
- Giới hạn của Semantic Scholar: 1 request/giây với key (đề: "The API enforces limits
  of 1 request per second ... Exceeding these triggers 429 errors, so implement delays
  and error handling").

### 3.8. Ngày nộp của bài được trích lấy từ Kaggle (`ref_dates_kaggle.py`)

- Mỗi 5000 bài trích dẫn khoảng 50 000 bài arXiv; hỏi ngày nộp qua API arXiv tốn
  khoảng 500–700 request, mỗi request cách nhau 3 s, và tranh nhịp với việc tải mã nguồn.
- Đề có nói: bộ dữ liệu Kaggle "can be used directly without any API calls". Script
  đọc file metadata của arXiv trên Kaggle theo dòng (không giải nén toàn bộ, ít RAM),
  lấy ngày của `v1`. Kaggle lỗi thì tự quay về hỏi arXiv.
- Ngày đã tra của thành viên trước được dùng lại (`--cache`), bài được nhiều người
  trích chỉ tra một lần.

### 3.9. Benchmark cả nhóm trên Colab (`chunked_crawl.py`, `snapshot.py`, `group_benchmark.py`)

Đề bắt đo trên Colab, thầy bắt đo trên **toàn bộ dữ liệu của nhóm** ("đánh giá trên
toàn bộ dữ liệu của nhóm"; "kết quả benchmark cần được chạy trên trọn bộ dữ liệu"),
mà Colab chỉ cho tối đa 12 giờ mỗi phiên. Lần chạy thử trên Drive còn cho thấy: mỗi
lần chạy lại tốn 9–13 phút chỉ để kiểm tra file `metadata.json` nào đã có, và file
tải dở bị xoá trên Drive rơi vào Thùng rác làm đầy Drive.

- **Dữ liệu nằm trên ổ của máy Colab** (`/content/...`), không phải Drive.
- **Chia từng bước 1000 bài**, mỗi bước chạy dưới `benchmark.py` để thời gian và RAM
  được đo đúng. Xong mỗi bước, phần mới được nén thành **một file `.tar.gz`** lưu lên
  Drive (chỉ phần thay đổi so với lần trước), không bao giờ ghi đè hay xoá gì trên Drive.
- **Runtime chết thì làm tiếp**: runtime mới khôi phục các bản lưu và chạy tiếp từ
  bước dở; mất tối đa một bước.
- **Chạy lần lượt từng khoảng**, mỗi lúc một crawler, vì điều khoản API của arXiv:
  giới hạn "apply to all of the machines under your control as a whole".
- **Thầy khuyến nghị chạy chung**: "thầy khuyến nghị các em tổng hợp list ID lại rồi
  chạy chung cho tiết kiệm tài nguyên tính toán".
- **`group_benchmark.py`** gộp log của mọi khoảng thành một bảng cho cả nhóm: thời
  gian, RAM đỉnh, dung lượng, số request, thời gian chờ arXiv, số lần bị từ chối.
  Tool này không gửi request nào.

### 3.10. Notebook Colab

- `src/chay-crawler-nhom-final.ipynb`: chạy chính thức cho cả nhóm, điền khoảng ID rồi
  chạy; key Semantic Scholar để trong *Secrets* của Colab, không ghi trong code.
- `src/chay-thu-crawler-nhom.ipynb`: chạy thử khoảng 100 ID trước khi chạy thật.
- Proxy và WARP là **tuỳ chọn** (`DUNG_PROXY`, `DUNG_WARP`). Thầy có nêu trong Q&A
  (sheet Milestone 3) rằng nhóm có thể dùng "các kỹ thuật tối ưu đa luồng hoặc VPN
  switch IP để vượt qua rate limit nhằm tăng tốc quá trình tải". Cần biết thêm: điều
  khoản API của arXiv ghi "You should not attempt to overcome these limits by
  increasing the number of machines used to make requests", và FAQ của Colab không
  cho kết nối tới proxy từ xa. Nếu runtime bị ngắt bất thường thì tắt hai công tắc này.

---

## 4. Những chỗ nhóm làm khác câu chữ của đề, và lý do

### 4.1. Nhịp tải mã nguồn: đề ghi 3 giây

Đề, phần tải mã nguồn: "Point automated downloads at this mirror and never at the
main site arxiv.org ... and use a descriptive User-Agent header with the same
3-second pacing."

Crawler hiện tải theo nhịp trang bulk data của arXiv (trung bình 2 request/giây,
nhiều file cùng lúc), nhanh hơn 3 giây/file. Căn cứ: thầy trả lời trong Q&A rằng có
thể "vận dụng các kỹ thuật tối ưu đa luồng ... để vượt qua rate limit nhằm tăng tốc
quá trình tải", và chính arXiv đề xuất nhịp "bursts at 4 requests per second" cho
`export.arxiv.org`.

Để so sánh: theo đúng 3 giây/file, mỗi khoảng 5000 ID (khoảng 7 900 phiên bản) mất
khoảng 6,6 giờ chỉ riêng phần tải. **Nhóm nên ghi rõ lựa chọn này trong báo cáo**,
kèm hai nguồn trên.

### 4.2. Không dùng bản lưu hàng loạt S3 / Google Cloud

Đề có nhắc `s3://arxiv/src/` (requester-pays) và "A free mirror of the same sources
also exists on Google Cloud Storage at gs://arxiv-dataset".

- **S3**: người tải trả tiền băng thông cho Amazon, cần tài khoản AWS, file được gom
  theo tháng (mỗi gói khoảng 500 MB, gồm nhiều bài), không lấy riêng từng phiên bản.
  Đề cũng ghi "Implementing a full repository-scale harvest is not part of this milestone."
- **Google Cloud `gs://arxiv-dataset`**: kiểm tra ngày 03/11/2026, thư mục
  `arxiv/arxiv/` chỉ có `pdf/`, `ps/`, `html/`, **không có mã nguồn TeX**, nên không
  dùng được cho Milestone 1 (cần `.tex`).

### 4.3. Không giữ file `.bbl`

Khoảng 9% phiên bản (mẫu 300 bài đầu của khoảng AQuan) chỉ có tài liệu tham khảo
trong file `.bbl`. Đề ghi rõ "extract only the .tex and .bib files from each source
archive" và "those are the only file types the working corpus keeps", nên nhóm làm
đúng đề và ghi các phiên bản này là giới hạn trong báo cáo.

---

## 5. Số đo thật để đưa vào báo cáo

Lần chạy 5200 ID của Thu (30/10/2026, cách chạy cũ: dữ liệu trên Drive, 4 kết nối):

| Chỉ số | Giá trị |
| :-- | :-- |
| Tổng thời gian | 23 513 s (6,53 giờ) |
| Phiên bản | 7 873 (7 307 tải được, 538 chỉ có PDF, 28 không có) |
| Thời gian chờ vì arXiv từ chối (429) | 10 815 s (**46%**) |
| Request tới arXiv | 9 696 (8 376 tải file, 1 320 API) |
| Dung lượng tải về / giữ lại (.tex, .bib) | 34,0 GB / 1,84 GB |
| RAM đỉnh | 481 MB |

Gần một nửa thời gian là chờ arXiv, và các lần từ chối kéo dài theo giờ trong ngày,
không phụ thuộc vào crawler; đây là ý chính khi giải thích runtime trong báo cáo.
Nhân viên arXiv cũng nói 429 là do tải chung của server.

---

## 6. Chạy thử nhanh

```bash
pip install -r src/requirements.txt
python src/main.py --range 2026-05 12338 2026-05 17337 --data-dir data --limit 10
python src/validate.py --data-dir data
```

Trên Colab: mở `src/chay-thu-crawler-nhom.ipynb`, điền tên, khoảng ID và `TU`, `DEN`
(khoảng 100 ID), rồi chạy từ trên xuống.
