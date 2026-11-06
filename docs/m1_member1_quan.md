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
| `ddeecb9` | 03/11 | Thêm đường đi qua AWS API Gateway (và nhiều tài khoản Webshare cho file lớn), 8 kết nối tải, chờ ngắn hơn và 1 request thăm dò sau mỗi lần arXiv từ chối |

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
- **File lớn bị đứt giữa chừng, arXiv không cho tải tiếp**: log chạy thật 5000 ID
  ngày 05/11 có các phiên bản `attempts = 6`, đã nhận được một số MiB tròn rồi kết
  thúc bằng `HTTP 500`, mỗi file 400–711 s: request xin phần còn thiếu (`Range`)
  bị trả 500 năm lần liền, và 500 không được tính như 503 nên file không được gác
  lại; phần lớn 1,07 giờ của bước *sweep* là chờ các file này. Từ bản sửa này:
  tải tiếp bị 500/502/504 thì bỏ phần đã có và tải lại cả file **một lần**, đứt nữa
  thì gác sang lượt sau; arXiv từ chối `Range` 3 lần mà chưa cho lần nào thì cả lần
  chạy không xin `Range` nữa (log: `[download] arXiv refused 3 resumed downloads ...`);
  500/502/504 cho request tải cả file được báo cho bộ giới hạn như 503 (lỗi 2 lần thì
  gác lại). `download_log.csv` có thêm `route` (đường của câu trả lời cuối) và
  `first_error` (lỗi đầu tiên, ví dụ vì sao file bị đứt).
- **Chờ ngắn và thăm dò** (`ddeecb9`): lần chờ đầu 5 s, gấp đôi mỗi lần bị từ chối
  tiếp, tối đa 2 phút; mỗi lần arXiv trả lời bình thường thì lần chờ sau giảm một nửa;
  hết chờ chỉ gửi **1 request thăm dò**, có trả lời bình thường thì các kết nối khác
  mới chạy tiếp. Lý do, từ log chạy thật 5200 ID: 46% thời gian là chờ, trong đó có 92
  phút liền mỗi lần 429 phải chờ 5 phút dù arXiv vẫn trả phần lớn file.
- **8 kết nối tải** (chỉnh bằng `KET_NOI` ở Bước 1 của notebook, tối đa 10): mỗi file
  mất khoảng 5,5 s mới về hết, nên 6 kết nối chỉ đạt khoảng 55% nhịp 2 request/giây.
  Nhịp không đổi: thêm kết nối không làm tăng số request mỗi giây.
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
- Proxy, WARP và AWS là **tuỳ chọn**, xem mục 3.11.

### 3.11. Đổi đường đi (IP): proxy Webshare, WARP, AWS API Gateway

Mục đích: khi arXiv trả 429 cho **một** địa chỉ IP, các request khác vẫn đi tiếp qua IP
khác, thay vì cả crawler phải dừng chờ. Cơ sở trong môn học là câu thầy trả lời trong
Q&A (sheet Milestone 3): nhóm có thể "vận dụng các kỹ thuật tối ưu đa luồng hoặc VPN
switch IP để vượt qua rate limit nhằm tăng tốc quá trình tải".

Mọi đường đi **dùng chung một bộ giới hạn** (mục 3.4): tổng số request của mọi IP cộng
lại vẫn theo đúng nhịp đó. Code ghi rõ: "more IPs add routes, not speed", tức thêm
IP là thêm đường dự phòng, không phải gửi nhanh hơn. Tất cả nằm trong `rate_limiter.py`.

| Đường đi | Ai thêm | Dùng cho | Bật bằng |
| :-- | :-- | :-- | :-- |
| Mạng của Colab | (mặc định) | Mọi request | Luôn có |
| **Proxy Webshare** (`ProxyPool`) | Quân, `a1582b5` | Xoay vòng các request gọi **API** arXiv (và file lớn, dòng cuối bảng) | `DUNG_PROXY` ở Bước 1 + secret `ARXIV_PROXIES` của riêng mỗi người (không ghi trong code, `proxies.txt` không được commit) |
| **Cloudflare WARP** | Quân, notebook `ca0fe44` | Tải **file nguồn**: một IP thứ hai bên cạnh IP của Colab | `DUNG_WARP` ở Bước 1; notebook tự cài `wgcf` + `wireproxy` (cổng SOCKS5 `127.0.0.1:40000`), crawler tự nhận nếu cổng mở |
| **AWS API Gateway** (`requests-ip-rotator`) | Quân, `ddeecb9` | Request API, và mặc định cả file nguồn, đi qua gateway của tài khoản AWS | `DUNG_AWS` (và `AWS_TAI_FILE`) ở Bước 1 + 2 secret `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`; thiếu key, thư viện hoặc gateway thì tự tắt |
| **Nhiều tài khoản Webshare cho file lớn** | Quân, `ddeecb9` | File AWS không chở được (> 10 MB) | `WEBSHARE_FILE_LON` ở Bước 1 (đặt `ARXIV_PROXY_LARGE_FILES=1`); cần cả AWS và `DUNG_PROXY` |

Các kỹ thuật đi kèm:

- **Dừng riêng từng đường** (Thu, `6ef1619`): 429 qua một đường thì chỉ đường đó nghỉ
  (15 s, gấp đôi, tối đa 5 phút), các đường khác đi tiếp. Chỉ khi không còn đường nào
  rảnh thì mọi request mới dừng như mục 3.4. Ghi log `route_pause` trong
  `arxiv_throttle_log.csv`.
- **Cách ly proxy hỏng** (`_quarantine`): proxy lỗi kết nối bị bỏ qua 5 phút, request
  được thử lại qua đường kế tiếp; lỗi proxy không tính là arXiv từ chối.
- **AWS chở không được thì đổi đường ngay**: API Gateway từ chối câu trả lời trên 10 MB
  và cắt request sau 29 s. Khi đó file được gửi lại qua Colab/WARP sau khi chờ lượt mới,
  và URL đó không đi qua AWS nữa (log `aws_fallback`). Gateway được khai báo kiểu nhị
  phân `*/*` để file `.tar.gz` không bị đổi thành chữ. arXiv trả 403 qua AWS 3 lần
  trong khi Colab/WARP vẫn tải được thì AWS bị tắt cho phần còn lại của lượt chạy.
- **Webshare theo tài khoản**: các IP cùng user/password được gom thành một tài khoản,
  mỗi tài khoản là một lượt trong vòng xoay để dùng đều băng thông (gói miễn phí 1 GB/tháng).
  Webshare trả 402 (hết băng thông) thì bỏ cả tài khoản; 407 thì bỏ IP đó; 3 IP của
  một tài khoản bị bỏ thì bỏ cả tài khoản.

**Cần biết trước khi bật:**

- **Điều khoản arXiv**: "the following rate limits apply to all of the machines under
  your control as a whole" và "You should not attempt to overcome these limits by
  increasing the number of machines used to make requests". Thầy cho phép trong môn
  học, nhưng điều khoản của arXiv vẫn ghi như vậy; nên ghi rõ cả hai trong báo cáo.
- **Colab**: FAQ của Colab không cho "connecting to remote proxies". Runtime bị ngắt
  bất thường khi đang bật proxy/WARP thì đặt `DUNG_PROXY`, `DUNG_WARP` thành `False`.
- **Chi phí**: AWS API Gateway tính phí theo request; Webshare miễn phí có 1 GB/tháng.
- **Trong notebook của repo**: Bước 1 có `DUNG_AWS`, `AWS_TAI_FILE`, `AWS_VUNG`,
  `WEBSHARE_FILE_LON`; Bước 4 nạp 2 key AWS từ *Secrets*, cài `requests-ip-rotator`,
  `boto3` (không nằm trong `requirements.txt`, chỉ cài khi bật AWS) và thử quyền API
  Gateway ở từng vùng; gateway thiếu thì `requests-ip-rotator` tự tạo lúc crawler khởi
  động. Không có key hay quyền thì crawler vẫn chạy bình thường, chỉ là không có đường AWS.

#### Nếu muốn bật đường AWS: code cần gì và những điều phải giữ

Ở mỗi vùng trong `AWS_VUNG` (biến `ARXIV_AWS_REGIONS`), `requests-ip-rotator` dùng lại
gateway có tên `https://export.arxiv.org - IP Rotate API`, chưa có thì tự tạo một cái.
Tài khoản đăng ký bằng mail trường nằm trong AWS Organization của trường: SCP của
Organization chặn API Gateway ở mọi vùng trừ `ap-southeast-2` (đo ngày 06/11), nên
`AWS_VUNG` mặc định là `ap-southeast-2`; để trống thì thư viện dùng danh sách vùng mặc
định và dừng ở vùng bị chặn đầu tiên. Đường AWS chỉ bật khi có đủ 3 thứ dưới đây; thiếu bất kỳ thứ nào thì
crawler chạy tiếp như không có AWS (thiếu key thì không in gì; có key mà khởi động
lỗi thì in `[proxy] Lỗi khởi động AWS API Gateway: ...`):

| Cần có | Ở đâu | Ghi chú |
| :-- | :-- | :-- |
| Thư viện `requests-ip-rotator` và `boto3` | Notebook tự cài ở Bước 4 khi bật `DUNG_AWS` | Không có trong `requirements.txt` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | *Secrets* của Colab (bật Notebook access) | Bước 4 chuyển vào biến môi trường của crawler; *Secrets* không tự thành biến môi trường |
| Quyền API Gateway ở các vùng `AWS_VUNG` | IAM của user (và SCP của Organization, nếu có) | Bước 4 thử quyền từng vùng và in lý do khi bị chặn |

Các công tắc liên quan (Bước 1 của notebook đặt chúng): `AWS_TAI_FILE = False` (tức
`ARXIV_AWS_FILES=0`) để chỉ cho request API đi qua AWS, file nguồn đi Colab/WARP (mặc
định: cả file nguồn); `WEBSHARE_FILE_LON` (tức `ARXIV_PROXY_LARGE_FILES=1`) để file trên
10 MB đi vòng xoay Webshare thay vì chỉ Colab/WARP.

Những điều phải giữ khi dùng AWS:

- **Key**: chỉ để trong *Secrets* của Colab, không dán vào ô code, không in ra, không
  commit. Nên dùng một user IAM riêng, chỉ có quyền với API Gateway, và xoá key đó khi
  nhóm làm xong đồ án. Lộ key AWS là người khác dùng được tài khoản và tính tiền vào đó.
- **Chi phí**: API Gateway tính tiền theo số request. Theo dõi trang Billing của AWS
  trong lúc chạy; xong việc thì **xoá các gateway** đã tạo, để không bị tính phí tiếp.
- **Tắt nhanh**: bỏ 2 biến key (hoặc không cài thư viện) là crawler không dùng AWS nữa,
  không cần sửa code.
- **Kiểm tra AWS có chạy không**: AWS bật thì lúc khởi động có dòng
  `[proxy] Đã khởi động AWS API Gateway thành công: N vùng ...`; không thấy dòng này là
  không có đường AWS. Trong lúc chạy, `[AWS] ...: gửi lại qua ...` và `aws_fallback`
  trong `arxiv_throttle_log.csv` là các file AWS không chở được, đã tải lại qua Colab/WARP;
  `[AWS] ... tắt AWS cho phần còn lại của bước này` là arXiv trả 403 qua AWS 3 lần.

---

## 4. Những chỗ nhóm làm khác câu chữ của đề, và lý do

### 4.1. Nhịp tải mã nguồn: đề ghi 3 giây

Đề, phần tải mã nguồn: "Point automated downloads at this mirror and never at the
main site arxiv.org ... and use a descriptive User-Agent header with the same
3-second pacing."

Crawler hiện tải theo nhịp trang bulk data của arXiv (trung bình 2 request/giây,
8 file cùng lúc), nhanh hơn 3 giây/file. Căn cứ: thầy trả lời trong Q&A rằng có
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

## 5. Tất cả công tắc và tham số

### 5.1. Notebook (Bước 1)

| Biến | Mặc định | Ý nghĩa |
| :-- | :-- | :-- |
| `CAC_KHOANG` | (phải điền) | Mỗi khoảng một dòng: tên và 4 cột trong sheet. Chạy lần lượt từng khoảng |
| `THU_MUC_LUU` | `chinh_thuc` (thử: `chay_thu`) | Thư mục trên Drive giữ bản lưu `.tar.gz` và kết quả |
| `DOAN` | 1000 | Số bài mỗi bước; runtime chết thì chỉ làm lại bước đang chạy |
| `NGAY_KAGGLE` | `True` | Ngày nộp của bài được trích lấy từ Kaggle thay vì API arXiv |
| `CHO_TOI_DA` | 30 | Phút arXiv từ chối liên tục thì dừng bước (tự thử lại một lần sau 15 phút) |
| `KET_NOI` | 8 | Số file tải cùng lúc, tối đa 10; nhịp vẫn 2 request/giây |
| `DUNG_PROXY` | `True` | Xoay proxy Webshare cho request API (cần secret `ARXIV_PROXIES`) |
| `DUNG_WARP` | `True` | Notebook tự cài WARP làm IP thứ hai cho việc tải file |
| `DUNG_AWS` | `True` | Đường AWS API Gateway (cần 2 secret AWS và gateway sẵn có; không có thì tự bỏ qua) |
| `AWS_TAI_FILE` | `True` | Khi có AWS: file nguồn < 10 MB cũng đi AWS (`False`: chỉ request API) |
| `AWS_VUNG` | `"ap-southeast-2"` | Vùng AWS được dùng, cách nhau bằng dấu phẩy (tài khoản mail trường chỉ được vùng này) |
| `WEBSHARE_FILE_LON` | `True` | Khi có AWS và proxy: file AWS báo > 10 MB đi xoay vòng Colab + WARP + từng tài khoản Webshare |
| `CHAP_NHAN_CON_THIEU` | `False` | `True`: Bước 6 trả mã 4 (còn vài file chưa tải được) vẫn đi tiếp sang Bước 7 |
| `FILE_MOI` | `github` | Lấy code từ nhánh `BRANCH`, hoặc nạp file `.py` mới chưa push |

### 5.2. `main.py` (chạy tay hoặc trong từng bước)

| Tham số | Ý nghĩa |
| :-- | :-- |
| `--range M1 ID1 M2 ID2` hoặc `--start ID --end ID` | Khoảng ID, viết như 4 cột trong sheet hoặc như hai ID |
| `--data-dir` | Thư mục dữ liệu (để ngoài repo; trên Colab dùng `/content/...`) |
| `--stages` | Chạy riêng một số bước: `discovery,metadata,download,references` |
| `--limit N` | Chạy thử N ID đầu |
| `--batch-size` | Số ID mỗi request API (mặc định 100) |
| `--retry-passes` | Số lượt thử lại phần còn thiếu (mặc định 2) |
| `--max-wait-minutes` | Phút arXiv từ chối liên tục thì dừng với mã 3 (mặc định 30) |
| `--no-arxiv-dates` | Không hỏi arXiv ngày nộp của bài được trích (để `ref_dates_kaggle.py` điền) |
| `--force`, `--user-agent` | Làm lại bước đã xong; đổi User-Agent gửi arXiv |

### 5.3. Biến môi trường

| Biến | Ý nghĩa |
| :-- | :-- |
| `S2_API_KEY` | Key Semantic Scholar của riêng mỗi người (1 request/giây khi có key) |
| `ARXIV_CONNECTIONS` | Số file tải cùng lúc (notebook đặt từ `KET_NOI`) |
| `ARXIV_PROXIES` | Danh sách proxy Webshare, cách nhau bằng dấu phẩy (notebook đọc từ secret) |
| `WARP_PORT` | Cổng SOCKS5 của WARP (mặc định 40000) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Bật đường AWS (mục 3.11) |
| `ARXIV_AWS_FILES` | `0`: file nguồn không đi qua AWS |
| `ARXIV_PROXY_LARGE_FILES` | `1`: file trên 10 MB đi vòng xoay Webshare |

---

## 6. Đọc log để biết kỹ thuật nào đã chạy

Phần lớn nằm trong `<data-dir>/logs/` (trên Drive: trong bản lưu của từng khoảng);
`chunk_journal.jsonl` ở `<data-dir>/work/`, `snapshot_log.csv` ở thư mục bản lưu trên Drive.

| File | Một dòng là | Dùng để |
| :-- | :-- | :-- |
| `run_log.csv` | Một bước của một lần chạy (bắt đầu, kết thúc, số giây) | Thời gian từng bước |
| `arxiv_api_log.csv` | Một request API arXiv | Số request, thời gian, lỗi của API |
| `download_log.csv` | Một phiên bản được xử lý | Trạng thái, số byte tải về và giữ lại, số lần thử |
| `references_log.csv` | Một bài mỗi lần lấy references | Trạng thái references, cách lấy (`/paper/batch` hay từng trang) |
| `pass_log.csv` | Một lượt | Còn thiếu gì sau mỗi lượt, để tính tỉ lệ thành công theo lượt |
| `arxiv_throttle_log.csv` | Một lần arXiv từ chối hoặc không trả lời | Thời gian chờ, xem bảng dưới |
| `work/chunk_journal.jsonl`, `snapshot_log.csv` | Một bước của `chunked_crawl.py`, một lần lưu/khôi phục Drive | Biết bước nào xong, lưu Drive mất bao lâu |

Cột `event` của `arxiv_throttle_log.csv`:

| `event` | Nghĩa |
| :-- | :-- |
| `pause` | Mọi request arXiv dừng chung một khoảng (cột `pause_seconds`) |
| `isolated` | Một file lỗi riêng, các file khác vẫn tải |
| `defer` | File lỗi lần thứ hai bị gác lại cho lượt sau |
| `recover` | arXiv trả lời bình thường trở lại: lần chờ sau về 5 s |
| `give_up` | Bị từ chối liên tục quá `--max-wait-minutes`: dừng với mã 3 |
| `route_pause` | Chỉ một đường (Colab, WARP, AWS, proxy) dừng, các đường khác đi tiếp |
| `aws_fallback` | AWS không chở được file (trên 10 MB, quá 29 s...): đã tải lại qua Colab/WARP |

`group_benchmark.py` gộp các file này cho cả nhóm (cột `arxiv_pause_seconds`,
`arxiv_pauses`, `arxiv_deferred`, `arxiv_route_pauses`...).

---

## 7. Sự cố thường gặp

| Thấy gì | Nghĩa là | Làm gì |
| :-- | :-- | :-- |
| `Crawler return code: 0` | Xong mọi khoảng | Sang Bước 7 |
| Mã **4** | Xong, còn vài file arXiv chưa trả được | 1–2 giờ sau *Run all* lại: chỉ làm phần còn thiếu |
| Mã **3** | arXiv từ chối liên tục (đã tự thử lại sau 15 phút) | 1–2 giờ sau *Run all* lại |
| Mã **130** | Bấm Stop | *Run all* lại khi muốn: làm tiếp từ bước dở |
| Mã **1** | Một bước lỗi 2 lần, hoặc không lưu được lên Drive | Đọc dòng lỗi ngay phía trên; Drive đầy thì dọn chỗ và thùng rác |
| `[arXiv] HTTP 429 ... pausing all arXiv requests for N s` | arXiv đang quá tải | Bình thường, cứ để chạy |
| `... put aside for a later pass` | File arXiv chưa trả được, đã gác lại | Bình thường, lượt sau tải lại |
| `[proxy] ... bỏ qua 5 phút` | Proxy đó lỗi kết nối | Cùng một proxy hiện mãi thì xoá khỏi secret |
| Runtime tự ngắt khi đang bật proxy/WARP | Có thể Colab chặn kết nối proxy | Đặt `DUNG_PROXY`, `DUNG_WARP` thành `False` |
| Bước 2 in `0 bản lưu` dù đã chạy trước đó | Bước 1 bị đổi hoặc sai tài khoản Drive | Dừng ngay, sửa lại Bước 1 kẻo crawl lại từ đầu |

---

## 8. Số đo thật để đưa vào báo cáo

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

## 9. Chạy thử nhanh

```bash
pip install -r src/requirements.txt
python src/main.py --range 2026-05 12338 2026-05 17337 --data-dir data --limit 10
python src/validate.py --data-dir data
```

Trên Colab: mở `src/chay-thu-crawler-nhom.ipynb`, điền tên, khoảng ID và `TU`, `DEN`
(khoảng 100 ID), rồi chạy từ trên xuống.
