"""Cấu hình ứng dụng, đọc từ file .env bằng pydantic-settings."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class BrowserEngine(StrEnum):
    """Browser engine dùng cho crawler."""

    CHROMIUM = "chromium"
    CAMOUFOX = "camoufox"


class Settings(BaseSettings):
    """Toàn bộ cấu hình của project.

    Mọi biến môi trường đều có thể ghi đè bằng file ``.env`` nằm cạnh project.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- Đường dẫn ----------------
    database_path: Path = Path("data/news.db")
    log_file: Path = Path("logs/crawler.log")
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    timezone: str = "Asia/Ho_Chi_Minh"

    # ---------------- Browser ----------------
    browser_engine: BrowserEngine = BrowserEngine.CHROMIUM
    browser_headless: bool = True
    browser_locale: str = "en-US"
    browser_user_agent: str = ""
    browser_viewport_width: int = 1440
    browser_viewport_height: int = 900
    browser_timeout_ms: int = 30_000
    trace_enabled: bool = False
    block_media: bool = True

    # ---------------- Camoufox (nghiên cứu fingerprint) ----------------
    camoufox_headless: bool = True
    camoufox_os: str = ""
    camoufox_humanize: float = 0.0
    camoufox_block_images: bool = True
    camoufox_block_webrtc: bool = True
    camoufox_i18n: str = "en-US"
    camoufox_executable_path: str = ""

    # ---------------- Crawler ----------------
    max_concurrent_sources: int = Field(default=4, ge=1, le=32)
    max_articles_per_source: int = Field(default=12, ge=1, le=200)
    request_delay_seconds: float = Field(default=1.5, ge=0.0)
    source_timeout_seconds: float = Field(default=90.0, gt=0.0)
    retry_attempts: int = Field(default=2, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=3.0, ge=0.0)
    respect_robots_txt: bool = True
    scroll_rounds: int = Field(default=3, ge=0, le=20)
    #: Bật nguồn nào sẽ được crawl (rỗng = tất cả).
    enabled_sources: str = ""
    #: Tắt nguồn cụ thể, phân tách bằng dấu phẩy — dùng cho site chặn bot
    #: (Cloudflare 403) vì ta không dùng kỹ thuật né chặn.
    disabled_sources: str = "theblock,beincrypto"

    @property
    def enabled_source_list(self) -> list[str]:
        return [name.strip() for name in self.enabled_sources.split(",") if name.strip()]

    @property
    def disabled_source_list(self) -> list[str]:
        return [name.strip() for name in self.disabled_sources.split(",") if name.strip()]

    # ---------------- Dedup ----------------
    #: Cùng nguồn, tiêu đề gần như giống nhau (sửa nhẹ, thêm/bớt từ).
    #: Đo trên dữ liệu thật: tin trùng 0.71–1.0, tin cùng chủ đề khác sự kiện 0.30–0.44.
    dedupe_jaccard_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    #: Khác nguồn nhưng cùng một sự kiện (tiêu đề được viết lại).
    #: 0.80 = rất thận trọng; đặt 0.0 để tắt hoàn toàn lớp này.
    dedupe_cross_source_overlap: float = Field(default=0.80, ge=0.0, le=1.0)
    #: Chỉ gom tin khác nguồn nếu thời gian đăng cách nhau không quá ngần này.
    dedupe_cross_source_window_hours: int = Field(default=36, ge=1, le=240)
    dedupe_window_days: int = Field(default=7, ge=1, le=90)

    # ---------------- Trend ----------------
    trend_min_score: float = Field(default=0.5, ge=0.0, le=1.0)
    trend_max_age_hours: float = Field(default=24.0, gt=0.0)
    trend_max_per_category: int = Field(default=5, ge=1, le=50)
    trend_freshness_half_life_hours: float = Field(default=6.0, gt=0.0)
    trend_topic_window_minutes: int = Field(default=60, ge=1)

    # ---------------- Telegram ----------------
    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_rate_limit_seconds: float = Field(default=1.5, ge=0.0)
    #: Số lần thử TỐI ĐA cho mỗi message (không phải số lần thử lại).
    telegram_max_retries: int = Field(default=3, ge=1, le=10)
    telegram_retry_base_delay: float = Field(default=2.0, ge=0.1)

    # ---------------- Scheduler ----------------
    auto_crawl_enabled: bool = True
    crawl_interval_minutes: int = Field(default=1440, ge=1)

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"LOG_LEVEL khong hop le: {value}")
        return level

    @field_validator("browser_user_agent", "camoufox_os", "camoufox_executable_path", mode="before")
    @classmethod
    def _strip_optional(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @property
    def resolved_database_path(self) -> Path:
        return self._resolve(self.database_path)

    @property
    def resolved_log_file(self) -> Path:
        return self._resolve(self.log_file)

    @property
    def trace_dir(self) -> Path:
        return self._resolve(Path("logs/traces"))

    @property
    def profile_dir(self) -> Path:
        return self._resolve(Path("profiles/default"))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def allowed_user_ids(self) -> frozenset[str]:
        """Danh sách user id được phép điều khiển bot (rỗng = dùng chat id)."""
        raw = self.telegram_allowed_user_ids.strip()
        ids = frozenset(part.strip() for part in raw.split(",") if part.strip())
        if ids:
            return ids
        if self.telegram_chat_id.strip():
            return frozenset({self.telegram_chat_id.strip()})
        return frozenset()

    @property
    def telegram_ready(self) -> bool:
        """True khi đủ token + chat id để gửi tin."""
        return bool(
            self.telegram_enabled
            and self.telegram_bot_token.strip()
            and self.telegram_chat_id.strip()
        )

    @property
    def camoufox_os_list(self) -> list[str] | None:
        raw = self.camoufox_os.strip()
        if not raw:
            return None
        return [part.strip().lower() for part in raw.split(",") if part.strip()]

    @staticmethod
    def _resolve(path: Path) -> Path:
        return path if path.is_absolute() else PROJECT_ROOT / path

    def ensure_directories(self) -> None:
        """Tạo các thư mục cần thiết nếu chưa có."""
        self.resolved_database_path.parent.mkdir(parents=True, exist_ok=True)
        self.resolved_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Trả về instance Settings dùng chung (cache theo process)."""
    return Settings()
