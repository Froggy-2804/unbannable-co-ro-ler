# UIT Confession Crawler

Crawler giao diện Facebook bằng Python + Playwright, dành cho một người dùng chạy trên máy cá nhân. Page mặc định: <https://www.facebook.com/UITconfess/>.

Đăng nhập thủ công trong browser profile riêng; lưu bài viết, bình luận, replies và tiến độ vào SQLite. Không sử dụng API nội bộ, tự nhập mật khẩu, giải CAPTCHA, giả fingerprint hoặc xoay tài khoản/proxy.

**Không có bảo đảm tránh khóa tài khoản hay lấy hết dữ liệu Facebook.** Tự động thu thập qua giao diện có rủi ro điều khoản: [chính sách Meta](https://developers.facebook.com/documentation/development/terms-and-policies/automated-data-collection.md). Chương trình dừng khi phát hiện mất phiên, checkpoint hoặc hạn chế truy cập.

## Cài đặt

Yêu cầu Python 3.11 trở lên. PowerShell trên Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[analysis]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

macOS/Linux: thay `.\.venv\Scripts\python.exe` bằng `.venv/bin/python`. Linux có thể cần `python -m playwright install --with-deps chromium` trong môi trường đã cài package.

## Chạy

### 1. Đăng nhập

```powershell
.\.venv\Scripts\python.exe -m uit_crawler login
```

Tự đăng nhập và hoàn thành xác thực trong cửa sổ trình duyệt. Chương trình chỉ quan sát trạng thái; không tự xử lý form đăng nhập hay kiểm tra bảo mật. Khi xác nhận được phiên đăng nhập, cửa sổ đóng và profile được lưu trên máy. Thời gian chờ mặc định là 600 giây, điều chỉnh bằng `login --timeout 1200`.

Facebook có thể chuyển trang nhiều lần sau đăng nhập hoặc xác thực. Bộ theo dõi chờ `DOMContentLoaded`, đọc lại trạng thái khi execution context bị hủy và chỉ kết hợp DOM/cookie của cùng một document ổn định. Nếu quay lại form đăng nhập hoặc checkpoint trong bước xác nhận, chương trình tiếp tục chờ bạn xử lý trong thời gian login cho phép; không đóng trình duyệt sớm.

Lỗi `Page.evaluate: Execution context was destroyed` do chuyển hướng đã được xử lý ở bộ kiểm tra phiên. Không cần xóa profile để áp dụng bản sửa. Các lỗi không liên quan đến chuyển hướng vẫn được báo; trong crawl, nếu không lấy được document ổn định trong 10 giây, chương trình tạm dừng với lý do `navigation_unstable` thay vì coi kiểm tra an toàn là thành công.

Có thể dùng Chrome/Edge đã cài với `login --channel chrome` hoặc `--channel msedge`; dùng cùng `--channel` khi chạy `crawl`.

### 2. Crawl một batch

```powershell
.\.venv\Scripts\python.exe -m uit_crawler crawl --max-posts 5 --max-scrolls 10
.\.venv\Scripts\python.exe -m uit_crawler status
```

Chương trình khám phá permalink, ghi hàng đợi trước mỗi lần cuộn, rồi mở từng bài để đọc nội dung và mở rộng bình luận/replies. Layout đăng nhập có thể dùng `aria-posinset` thay vì `role="article"` cho bài viết; link thời gian có thể chỉ cung cấp permalink sau khi hover. Crawler hover phần metadata rồi đọc lại URL, không đoán ID từ href placeholder.

**Không mở hoặc thu thập nội dung/URL của link preview đính kèm.** Nút “Xem thêm” chỉ được bấm khi thuộc phần văn bản bài viết hoặc bình luận đã nhận diện, không thuộc link/card preview. Các nút mở thêm bình luận/replies và chọn chế độ bình luận vẫn được hỗ trợ. Không bấm Like, Reply, Comment, Share hay gửi nội dung.

Link xuất hiện trong chính văn bản bài viết/bình luận được giữ như văn bản, không được đưa vào hàng đợi hay mở trang đích. Dữ liệu attachment đã lưu từ trước vẫn được bảo toàn khi xuất; crawler không thu thập thêm URL attachment.

Mặc định xử lý tối đa 20 bài mỗi batch, 40 lần cuộn khi khám phá, tối đa 200 lần hover metadata trong một lượt khám phá và 200 thao tác mở rộng/cuộn mỗi bài. `--max-actions` điều chỉnh cả hai ngân sách thao tác. `max_permalink_hovers` nghĩa là đã hết ngân sách hover, không phải đã lấy hết lịch sử. Đây là **giới hạn công việc**, không phải ngưỡng chống ban.

Chạy tiếp hàng đợi mà không cuộn lại Page:

```powershell
.\.venv\Scripts\python.exe -m uit_crawler crawl --skip-discovery --max-posts 20
```

Có thể tiếp tục chạy các batch. Tăng `--max-scrolls` để khám phá sâu hơn, nhưng feed xếp hạng và giới hạn giao diện vẫn có thể khiến lịch sử không đầy đủ.

### 3. Xuất dữ liệu

```powershell
.\.venv\Scripts\python.exe -m uit_crawler export --format jsonl --output exports\run-001.jsonl
.\.venv\Scripts\python.exe -m uit_crawler export --format csv --output exports\run-001-csv
```
JSONL chứa các loại `post`, `comment`, `comment_observation`, `run`, `job`. Khi thêm cờ `--include-analysis`, xuất thêm `confession`, `comment_analysis`, `comment_analysis_target`, `analysis_run`.
CSV tạo `posts.csv`, `comments.csv`, `comment_observations.csv`, `runs.csv`, `jobs.csv` (và `confessions.csv`, `comment_analyses.csv`, `comment_analysis_targets.csv`, `analysis_runs.csv` nếu có `--include-analysis`).

Không ghi đè file đã tồn tại hoặc thư mục CSV không rỗng. JSONL giữ nguyên nội dung; CSV thêm dấu nháy đơn trước ô có nguy cơ được bảng tính thực thi như công thức.

```powershell
# Xuất dữ liệu kèm kết quả phân tích sentiment:
.\.venv\Scripts\python.exe -m uit_crawler export --format csv --output exports\run-001-csv --include-analysis
```

### 4. Phân tích Sentiment bình luận bằng Ollama (`gpt-oss:20b`)

Phân loại cảm xúc của bình luận và replies theo từng confession trong bài viết mà bình luận đó đề cập, sử dụng mô hình local `gpt-oss:20b` qua Ollama.

#### Lựa chọn 1: Dùng Ollama Local (Không cần API key, hoàn toàn miễn phí)
1. Cài đặt Ollama từ <https://ollama.com/download>.
2. Tải mô hình `gpt-oss:20b` (yêu cầu máy có khoảng 16GB+ RAM/VRAM):
   ```powershell
   ollama pull gpt-oss:20b
   ollama run gpt-oss:20b
   ```

#### Lựa chọn 2: Dùng Ollama Cloud (cần API key; hạn mức theo tài khoản)
1. Đăng ký tài khoản miễn phí tại <https://ollama.com>.
2. Lấy API key tại <https://ollama.com/settings/keys>.
3. Cung cấp API key bằng một trong các cách sau:
   - **Cách 1 (Khuyên dùng):** Tạo file `.env` ở thư mục gốc dự án (hoặc copy từ `.env.example`):
     ```bash
     OLLAMA_API_KEY=dien_api_key_cua_ban_vao_day
     ```
   - **Cách 2:** Đặt biến môi trường trong PowerShell:
     ```powershell
     $env:OLLAMA_API_KEY = "dien_api_key_cua_ban_vao_day"
     ```
   - **Cách 3:** Truyền trực tiếp qua cờ `--api-key <key>` khi chạy lệnh.

Key từ `.env`/biến môi trường chỉ được tự động gửi tới origin HTTPS `https://ollama.com` (port mặc định hoặc 443), không gửi sang Ollama local hoặc URL tùy chỉnh. `--cloud` không kết hợp với URL tùy chỉnh ngoài origin này. Nếu chủ động truyền `--api-key` cho endpoint khác, endpoint phải dùng HTTPS, ngoại trừ loopback.

#### Quy tắc phân tích:
- **Chỉ phân loại comment và reply**, không phân loại bài viết hay bản thân confession.
- **Chỉ xử lý bài viết có mã confession hợp lệ** (`#2518`, `#3694`); bài thông báo, sự kiện âm nhạc, meme không có mã confession sẽ được tự động bỏ qua.
- **3 nhãn cảm xúc:** `positive` (tích cực), `negative` (tiêu cực), `neutral` (trung tính). Khi chưa đủ căn cứ, `sentiment` là `null` và kết quả cần kiểm tra (`needs_review`), không ép thành neutral.
- **Hỗ trợ đa đối tượng & ngữ cảnh hội thoại:** nhận diện comment nhắc mã trực tiếp hoặc suy luận qua chuỗi comment cha; một comment có thể liên kết nhiều confession với cảm xúc độc lập.
- **Bình luận không có chữ** (`content_kind = 'media'` như ảnh/sticker) được ghi nhận là `skipped_empty` mà không tốn lượt gọi model.
- Post/comment chưa mở rộng đầy đủ được giữ lại để kiểm tra, không phân loại như văn bản đầy đủ. `partial` do chưa mở hết comment không ngăn phân tích post có nội dung đã đọc đủ.
- Evidence phải là đoạn trích nguyên văn. JSON sai schema, target trùng/không hợp lệ hoặc evidence sai được ghi nhận là lỗi, không tự cắt/sửa quote để chấp nhận.
- Ollama Cloud dùng JSON mode; toàn bộ JSON Schema được đưa vào system prompt trước khi tính ngân sách context. `reason_code`/`review_required` chỉ ở cấp kết quả comment (hoặc `results[i]` trong batch), không nằm trong `targets[i]`. JSON mode không cưỡng chế schema như validator: response vẫn sai sẽ được ghi là error, không tự bỏ field hoặc di chuyển reason giữa các cấp.
- Batch chỉ gom comment cùng post. Comment bị model bỏ sót được thử lại riêng tối đa một lần; lỗi fallback được giữ đúng nguyên nhân. Lỗi xác thực, hết hạn mức/rate limit hoặc mất kết nối dừng run, không đánh dấu lỗi toàn bộ hàng đợi còn lại.

#### Các lệnh phân tích:

```powershell
# 1. Kiểm tra trước khối lượng công việc (dry-run):
# Local:
.\.venv\Scripts\python.exe -m uit_crawler analyze --dry-run
# Cloud:
.\.venv\Scripts\python.exe -m uit_crawler analyze --cloud --dry-run

# 2. Phân tích một batch bình luận:
# Local:
.\.venv\Scripts\python.exe -m uit_crawler analyze --max-comments 50
# Cloud (gom tối đa 8 comment cùng post mỗi request):
.\.venv\Scripts\python.exe -m uit_crawler analyze --cloud --max-comments 50 --batch-size 8
# Cloud (xử lý toàn bộ comment còn lại):
.\.venv\Scripts\python.exe -m uit_crawler analyze --cloud --all --batch-size 8
# 3. Phân tích riêng cho một bài viết cụ thể:
.\.venv\Scripts\python.exe -m uit_crawler analyze --cloud --post-key POST_KEY --max-comments 50

# 4. Thử lại các bình luận từng bị lỗi:
.\.venv\Scripts\python.exe -m uit_crawler analyze --cloud --retry-failed

# 5. Xem thống kê tiến độ và phân bố cảm xúc theo confession:
.\.venv\Scripts\python.exe -m uit_crawler analysis-status

# 6. Đánh giá độ chính xác với tập nhãn chuẩn (gold JSONL):
.\.venv\Scripts\python.exe -m uit_crawler analysis-evaluate --gold data\gold.jsonl --output data\eval_report.json
```

Sau khi cập nhật code, cập nhật dependency bằng `.\.venv\Scripts\python.exe -m pip install -e ".[analysis]"`.

**Context:** sử dụng `tiktoken>=0.12` với encoding `o200k_harmony`, chừa ngân sách output, framing và reasoning. Đây là ước lượng có dự phòng, không phải phép đếm chính xác chat template của Ollama. Encoding có thể cần tải dữ liệu công khai ở lần đầu; thiếu tokenizer/encoding sẽ báo prerequisite, không dùng heuristic thay thế. Batch quá dài được tách về từng comment; comment đơn vẫn quá dài được lưu `needs_review/context_limit`, không gọi model và không cắt nội dung.

**Cache và dữ liệu thay đổi:** fingerprint bao gồm context, completeness, model/digest, `think`, prompt/examples/schema và generation options; không bao gồm batch size. Thay đổi contract sau cập nhật có thể khiến các kết quả cũ được phân tích lại, nên bắt đầu bằng batch nhỏ thay vì chạy ngay `--all`. Thiếu model digest thì không xác nhận cache hiện hành. Khi nguồn thay đổi, toàn bộ kết quả của post được đánh dấu `stale` theo hướng bảo thủ; kết quả tính từ snapshot cũ không được ghi như hiện hành (`stale_conflict`).

**Đọc và thống kê:** `--dry-run` mở database hiện có ở chế độ chỉ đọc, không migration/inference/ghi dữ liệu. Khi metadata model không truy cập được, `model_digest` là `null` và không chứng nhận cache; số queued có thể cao hơn lượt chạy khi model hoạt động. Viewer và API thống kê cũng chỉ đọc; database cũ chưa có schema analysis tương thích vẫn xem được nội dung nguồn. Thống kê chính thức và evaluation chỉ tính analysis `success`; review/error/stale báo riêng. Anonymous observations giữ đủ `run_id`, không bảo đảm dedup xuyên run.

**Migration:** khi cần rebuild bảng analysis cũ, tạo SQLite online backup cạnh database dạng `crawler.sqlite3.bak`; nếu đã tồn tại thì dùng `.bak.1`, `.bak.2`, không ghi đè backup. Giữ các analysis/evidence cũ; anonymous legacy không đủ run provenance được giữ là stale, sentiment legacy ngoài ba lớp được chuyển sang review. Migration không xóa post/comment nguồn hoặc browser profile.

Batching giảm việc lặp lại context trong một số request, nhưng không bảo đảm một tỷ lệ tiết kiệm token hay mức tăng tốc cố định. Cần benchmark cùng tập comment và kiểm tra nhãn trên gold do con người gán; test/smoke không chứng minh accuracy của model.

### 5. Giao diện trực quan (Web UI)

Xem bài viết, confessions, bình luận và kết quả phân tích sentiment trên giao diện web trực quan:

```powershell
.\.venv\Scripts\python.exe -m uit_crawler ui
```

Lệnh này tự động mở trình duyệt tại `http://127.0.0.1:8765/`:
- **Danh sách bài viết cuộn được ở bên trái:** Hiển thị thẻ confession (`#3694`), ngày giờ, đoạn trích và số lượng bình luận.
- **Chi tiết bài viết ở bên phải:** Bấm vào bất kỳ bài nào để xem toàn bộ nội dung bài viết và nút "Mở trên Facebook ↗".
- **Thẻ Confessions trong bài:** Nếu bài viết gồm nhiều confession, các đoạn confession được tách riêng hiển thị ở đầu bài.
- **Lọc bình luận theo confession:** Các nút lọc trên đầu danh sách bình luận cho phép lọc nhanh comment của confession `#2518`, `#2519` hoặc xem tất cả.
- **Huy hiệu Sentiment và Bằng chứng:** Mỗi bình luận hiển thị badge cảm xúc tương ứng (`#2518 · tích cực`, `tiêu cực`, `trung tính`, `chờ kiểm tra`) kèm hộp "🔍 Bằng chứng phân tích" có thể bấm mở để xem đoạn trích dẫn chứng minh.
- **Cây bình luận và mở replies thủ công:** Các bình luận hiển thị bên dưới. Các câu trả lời (replies) được thu gọn mặc định; bấm nút **"Xem X câu trả lời"** để mở ra hoặc bấm **"Thu gọn câu trả lời"** để đóng lại như trên Facebook.
- **Tìm kiếm và sắp xếp:** Thanh tìm kiếm theo từ khóa/mã confession (#3690) và menu lọc theo bài mới nhất, nhiều bình luận nhất, cũ nhất.
- **Đọc đồng thời:** Giao diện mở SQLite ở chế độ read-only, không tự migration từ request xem dữ liệu; kết quả phân tích hết hạn có badge riêng và không tham gia bộ lọc confession hiện hành.

Tùy chọn:
- `--port <cổng>`: Đổi cổng (mặc định: `8765`).
- `--no-browser`: Không tự động bật cửa sổ trình duyệt.

### 6. Xóa dữ liệu để crawl lại từ đầu

Khi muốn dọn sạch database để crawl lại từ đầu:

```powershell
# Cách 1: Xóa bài/bình luận đã crawl, GIỮ LẠI phiên đăng nhập Facebook (Khuyên dùng)
.\.venv\Scripts\python.exe -m uit_crawler reset

# Cách 2: Xóa toàn bộ kể cả phiên đăng nhập (phải login lại)
.\.venv\Scripts\python.exe -m uit_crawler reset --all
```

Thêm `-y` nếu không muốn hiện câu hỏi xác nhận.

## Cập nhật và tiếp tục sau gián đoạn

- Tắt chương trình giữa chừng: dữ liệu đã checkpoint vẫn còn. Lần crawl sau đưa job đang chạy dở về `pending`.
- Bình luận có ID nguồn: upsert theo bài + ID, không gộp chỉ vì nội dung giống nhau.
- Nội dung đã mở rộng không bị bản rút gọn ở lần sau ghi đè. Bản chỉnh sửa được đọc đầy đủ có thể thay thế nội dung cũ.
- Không có ID nguồn: lưu thành quan sát cục bộ, phân biệt rõ với Facebook ID. Không thể bảo đảm chống trùng xuyên các lần chạy cho nhóm này.
- Bài đã quan sát được xem lại sau 24 giờ mặc định. `--revisit-hours 0` cho phép xem lại ngay, nhưng job `pending` vẫn được ưu tiên.
- Để đọc lại đúng một bài ngay, truyền `crawl --post URL`; có thể lặp `--post`. URL phải là permalink Facebook được hỗ trợ. Chế độ này bỏ qua khám phá feed.
- Job lỗi không tự lặp vô hạn. Sau khi kiểm tra nguyên nhân, dùng `--retry-failed`.
- Không suy diễn bài hoặc bình luận đã bị xóa chỉ vì một lần đọc không thấy nó.

Ví dụ chọn một permalink để cập nhật ngay:

```powershell
$postUrl = Read-Host "Dan permalink bai viet UIT Confession"
.\.venv\Scripts\python.exe -m uit_crawler crawl --post $postUrl
```

## Dừng khi Facebook hạn chế

Khi gặp mất phiên, checkpoint, CAPTCHA hoặc tín hiệu hạn chế, chương trình:

1. Giữ dữ liệu đã ghi và tiến độ.
2. Đánh dấu run là `paused`.
3. Ghi `data/paused.json`, chặn những lần crawl tiếp theo trước khi mở trình duyệt.

Chạy `login`, tự kiểm tra tài khoản và giải quyết yêu cầu của Facebook. Chỉ khi phiên đăng nhập sạch được xác nhận, lệnh login mới xóa trạng thái pause. Không dùng việc đăng nhập lại để cố tiếp tục khi hạn chế vẫn còn hiệu lực.

`Ctrl+C` kết thúc có lưu trạng thái; dừng cưỡng bức cũng được phục hồi từ SQLite ở lần chạy sau. Chỉ một tiến trình login/crawl được sở hữu cùng thư mục dữ liệu.

## Phạm vi và giới hạn dữ liệu

| Trạng thái | Ý nghĩa |
|---|---|
| `observed` | Không còn tiến triển hoặc nút mở rộng được nhận diện trong lần quan sát; không có nghĩa là đã lấy mọi bình luận Facebook |
| `partial` | Chạm giới hạn thao tác, nút không nhận diện được, không tiến triển khi còn nút, hoặc không chọn được chế độ bình luận |
| `unavailable` | Giao diện báo nội dung không khả dụng; không phải xác nhận đã xóa |
| `error` | Không nhận diện được cấu trúc cần thiết hoặc lỗi vận hành; không ghi nhận là thành công rỗng |

- Selector hỗ trợ `role="article"`, `aria-posinset` và nhãn tiếng Anh/Việt; giao diện Facebook thay đổi có thể cần cập nhật. Khi mở permalink, crawler chờ nội dung bài được render, không chỉ chờ HTML ban đầu.
- Replies chỉ có `parent_source_id` khi permalink hoặc cấu trúc lồng nhau cung cấp bằng chứng; không đoán quan hệ từ nội dung.
- `published_text` có thể là thời gian tương đối trên giao diện. `published_at` chỉ có khi DOM cung cấp thời gian máy đọc được; không tự gán thời gian crawl thành thời gian đăng.
- Bình luận chỉ chứa ảnh, sticker hoặc GIF (không có chữ) được lưu với `content_kind = 'media'`, `text = ''` (hoặc emoji), giữ nguyên ID bình luận, permalink và quan hệ replies cha–con. Crawler không tải file ảnh và không bấm vào ảnh để mở trang media. Bình luận có chữ thông thường có `content_kind = 'text'`. Cấu trúc không nhận diện được bất kỳ nội dung nào mới bị coi là lỗi layout.
- Các cột `complete` và `history_complete` luôn là `0`: UI không cung cấp bằng chứng về độ đầy đủ tuyệt đối.
- `--action-delay` điều chỉnh nhịp thao tác, không phải kỹ thuật tránh phát hiện hay bảo đảm an toàn.

## Dữ liệu riêng tư

Mặc định crawler sẽ thu thập tên hiển thị của người bình luận (`author_name`). Nếu không muốn lưu tên, thêm cờ `--no-authors`. Dữ liệu tên đã lưu trước đó trong database sẽ không bị tự động xóa khi chạy lại với `--no-authors`.

Thư mục mặc định:

```text
data/
  browser-profile/    Phiên đăng nhập nhạy cảm
  crawler.sqlite3    Dữ liệu, hàng đợi và lịch sử run
  paused.json        Chỉ xuất hiện khi cần kiểm tra thủ công
  .crawler.lock      Khóa tiến trình, không xóa để ép chạy song song
```

`data/`, `exports/`, database và `.venv/` đã được bỏ qua trong Git. Không chia sẻ browser profile; nó có thể cho phép sử dụng phiên tài khoản. Windows dùng quyền thư mục hiện có của tài khoản hệ điều hành; chương trình không tuyên bố mã hóa profile.

Đổi thư mục bằng tham số toàn cục, đặt trước lệnh: `python -m uit_crawler --data-dir D:\Private\uit-data login`. Với đường dẫn tùy chỉnh, tự bảo đảm nó không bị commit hoặc đồng bộ/chia sẻ ngoài ý muốn.

## Mã thoát

| Mã | Ý nghĩa |
|---|---|
| `0` | Lệnh hoàn tất; với crawl, batch chỉ có trạng thái observed hoặc không có job đến hạn. Không xác nhận toàn bộ lịch sử |
| `1` | Lỗi vận hành, URL/layout không được hỗ trợ hoặc lỗi output analysis; lỗi trong khi inference dừng run với nguyên nhân thật |
| `2` | Crawl/login dừng để kiểm tra thủ công; analysis lỗi preflight/cấu hình; argparse cũng dùng mã này cho cú pháp không hợp lệ |
| `3` | Crawl có bài partial/unavailable; analysis có ca cần review hoặc xung đột nguồn (`stale_conflict`) |
| `130` | Người dùng ngắt bằng Ctrl+C |

## Kiểm tra

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Browser regression dùng Chromium với trang fixture được chặn mạng và trả nội dung cục bộ. Chúng kiểm tra hành vi DOM, an toàn thao tác và lưu trữ; không chứng minh khả năng truy cập Facebook bằng tài khoản thật. Luồng đã đăng nhập trên Page mục tiêu cần người dùng tự đăng nhập để kiểm chứng.
