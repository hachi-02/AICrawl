# AI & Crypto News Crawler

Crawler tin AI và Crypto chạy bằng Python, Playwright/Camoufox, SQLite và
Telegram. Ứng dụng crawl nhiều nguồn song song, chuẩn hóa URL, chống trùng,
tính điểm xu hướng và chỉ gửi những tin chưa từng báo.

## Tính Năng

- Crawl 10 nguồn đang hoạt động bằng browser thật, có timeout và retry riêng.
- Tôn trọng `robots.txt`, crawl-delay và deny pattern của từng website.
- Không giả User-Agent và không dùng flag che automation để vượt chặn.
- Dedup theo canonical URL, content hash và độ giống tiêu đề.
- Gom nhiều nguồn đưa cùng một tin vào một bản ghi `news`.
- Tính trend score theo độ mới, từ khóa, số nguồn và mật độ chủ đề.
- Gửi Telegram dạng text, retry lỗi mạng và không gửi lại tin đã báo.
- Scheduler và các lệnh `/latest`, `/ai`, `/crypto`, `/trending`, `/status`,
  `/crawl`, `/help`.
- Hỗ trợ Chromium để vận hành và Camoufox để nghiên cứu fingerprint.

## Nguồn Tin

| Nhóm | Nguồn đang bật |
| --- | --- |
| AI | TechCrunch, VentureBeat, The Verge, MIT Technology Review, WIRED |
| Crypto | CoinDesk, Cointelegraph, Decrypt, Bitcoin Magazine, CryptoSlate |

The Block và BeInCrypto có adapter nhưng mặc định nằm trong
`DISABLED_SOURCES`: cả hai trả HTTP 403 qua Cloudflare trong môi trường kiểm
thử. Project không cố vượt CAPTCHA hoặc cơ chế bảo vệ của website.

## Kiến Trúc

```text
Scheduler / CLI / Telegram command
                |
                v
       Playwright source adapters
                |
                v
 Normalize -> Deduplicate -> SQLite
                            |
                            v
                    Trend scoring
                            |
                            v
                 Telegram (optional)
```

Các module chính:

- `app/crawler/`: browser lifecycle, robots.txt, source adapters, concurrency.
- `app/dedupe/`: canonical URL, hash, token similarity và batch index.
- `app/database/`: migration SQLite và repository async.
- `app/trend/`: công thức trend score.
- `app/telegram/`: formatter, notifier, command handlers và scheduler.
- `app/main.py`: pipeline và CLI.

## Yêu Cầu

- Python 3.11 trở lên; project đã được kiểm thử với Python 3.13.
- Windows, Linux hoặc macOS.
- Kết nối Internet để cài browser và crawl nguồn tin.
- Telegram bot token/chat ID nếu muốn chạy chế độ `serve`.

## Cài Đặt

PowerShell trên Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m camoufox fetch
Copy-Item .env.example .env
```

Camoufox là tùy chọn. Nếu chỉ crawl bằng Chromium thì có thể bỏ qua lệnh
`python -m camoufox fetch`.

Khởi tạo database:

```powershell
python -m app.main init-db
```

SQLite migration chạy tự động và idempotent mỗi khi ứng dụng khởi động.

## Cấu Hình

Ứng dụng đọc `.env` tại thư mục project. Bắt đầu từ `.env.example` và không
commit token thật.

Cấu hình tối thiểu để crawl:

```dotenv
BROWSER_ENGINE=chromium
BROWSER_HEADLESS=true
RESPECT_ROBOTS_TXT=true
DISABLED_SOURCES=theblock,beincrypto
TELEGRAM_ENABLED=false
```

Cấu hình Telegram:

```dotenv
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:your-token
TELEGRAM_CHAT_ID=@your_channel_or_chat_id
TELEGRAM_ALLOWED_USER_IDS=123456789
CRAWL_INTERVAL_MINUTES=10
```

`TELEGRAM_ALLOWED_USER_IDS` là danh sách Telegram user ID được phép điều khiển
bot, phân tách bằng dấu phẩy. Nếu để trống, ứng dụng dùng `TELEGRAM_CHAT_ID`.

Chọn nguồn:

```dotenv
# Rỗng nghĩa là cho phép mọi adapter
ENABLED_SOURCES=
# Danh sách này luôn bị loại, kể cả khi dùng --sources
DISABLED_SOURCES=theblock,beincrypto
```

## Lệnh CLI

Chạy một vòng nhưng không gửi Telegram:

```powershell
python -m app.main once --dry-run
```

`--dry-run` vẫn crawl, dedup, tính trend và ghi SQLite; nó chỉ bỏ bước gửi
Telegram.

Chỉ crawl một nhóm hoặc một số nguồn:

```powershell
python -m app.main once --dry-run --category ai
python -m app.main once --dry-run --sources coindesk,theverge
```

Xem trạng thái database:

```powershell
python -m app.main stats
```

Chạy Telegram bot và scheduler:

```powershell
python -m app.main serve
```

Không truyền subcommand cũng tương đương `serve`:

```powershell
python -m app.main
```

Probe fingerprint browser:

```powershell
python -m app.main probe --engine chromium
python -m app.main probe --engine camoufox
```

Báo cáo được lưu tại `logs/traces/fingerprint_<engine>.json`. Camoufox chỉ dùng
cho nghiên cứu/so sánh fingerprint, không dùng để vượt WAF hay CAPTCHA.

## Dedup

Pipeline kiểm tra theo thứ tự:

1. Canonical URL: bỏ tracking query, fragment và biến thể trailing slash.
2. Content hash: cùng nguồn và tiêu đề chuẩn hóa giống nhau.
3. Cùng nguồn: Jaccard token mặc định `>= 0.70`.
4. Khác nguồn: overlap tiêu đề mặc định `>= 0.80`, trong cửa sổ 36 giờ.

Đặt `DEDUPE_CROSS_SOURCE_OVERLAP=0.0` để tắt lớp fuzzy khác nguồn. Simhash
64-bit vẫn được lưu để phân tích nhưng không dùng làm hard gate vì tiêu đề ngắn
có độ dao động lớn.

`source_count` là số tên nguồn phân biệt, không phải số URL. URL phụ được lưu
trong `news_sources`; crawl lại cùng URL không tạo dòng phụ.

## Trend Score

```text
0.35 * freshness
+ 0.30 * keyword
+ 0.20 * source_count
+ 0.15 * topic_volume
```

Mặc định chỉ tự động gửi tin có điểm từ `0.5`, tuổi tối đa 24 giờ và tối đa 5
tin cho mỗi category trong một vòng.

## Telegram Delivery

- Tin chỉ được đánh dấu `is_reported=1` sau khi Telegram xác nhận gửi thành công.
- Delivery được ghi theo `(news_id, chat_id)` để chống gửi trùng.
- Lỗi mạng có exponential backoff; lỗi cuối cùng giữ tin ở trạng thái chưa gửi.
- Nếu message nhiều chunk chỉ gửi được một phần, cả nhóm không bị đánh dấu thành
  công và sẽ được thử lại ở vòng sau.
- Các lần gọi concurrent được khóa để cùng một tin không gửi đồng thời nhiều lần.

## Kiểm Thử

```powershell
python -m pytest tests -q
```

Suite bao phủ normalizer, dedup, repository, crawler config/link filtering,
trend score, Telegram delivery, formatter và tích hợp pipeline.

Kết quả smoke test gần nhất:

- 10/10 nguồn đang bật crawl thành công trong một vòng sạch.
- 98 bài được ghi ở vòng đầu.
- Vòng kế tiếp nhận diện phần lớn bài là trùng, không tạo canonical URL trùng và
  không tăng `source_count` giả.
- Camoufox `152.0.4-beta.31` fetch và probe thành công.

Số bài có thể thay đổi theo thời điểm và nội dung trang nguồn.

## Dữ Liệu Và Log

- Database: `data/news.db`.
- Log xoay vòng: `logs/crawler.log`.
- Playwright trace khi `TRACE_ENABLED=true`: `logs/traces/*.zip`.
- Fingerprint report: `logs/traces/fingerprint_*.json`.

Các đường dẫn này đã nằm trong `.gitignore` khi chứa dữ liệu runtime.

## Xử Lý Sự Cố

Browser Chromium chưa được cài:

```powershell
python -m playwright install chromium
```

Camoufox chưa có binary:

```powershell
python -m camoufox fetch
```

Nguồn chậm hoặc timeout:

- Xem `logs/crawler.log` để biết timeout xảy ra ở listing hay bài viết.
- Giảm `MAX_ARTICLES_PER_SOURCE` hoặc tăng `SOURCE_TIMEOUT_SECONDS`.
- CoinDesk, WIRED và The Verge đã có giới hạn/budget riêng trong adapter.
- Một nguồn lỗi không làm dừng các nguồn còn lại.

Telegram scheduler không tồn tại thường do cài thiếu extra `job-queue`. Cài lại:

```powershell
python -m pip install -r requirements.txt
```

Không xóa `data/news.db` trong môi trường đang dùng thật nếu chưa backup. Với
database phát triển có thể xóa file rồi chạy lại `python -m app.main init-db`.

## Nguyên Tắc Vận Hành

- Không giả User-Agent mặc định; browser dùng UA gốc của engine.
- Không dùng flag che `navigator.webdriver` hoặc vô hiệu sandbox.
- Không cố vượt Cloudflare/CAPTCHA.
- Tôn trọng `robots.txt` và crawl-delay.
- Không log Telegram token.
- Không gửi ảnh; Telegram dùng text và URL nguồn.
