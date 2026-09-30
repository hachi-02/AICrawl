"""Telegram Bot: gửi tin nổi bật và cung cấp lệnh điều khiển crawler.

Gồm 3 phần:

* :class:`TelegramNotifier` - gửi tin có retry, chỉ đánh dấu ``is_reported``
  sau khi Telegram xác nhận thành công.
* :class:`NewsTelegramBot` - đăng ký các lệnh ``/latest /ai /crypto /trending
  /status /crawl /help /start``.
* :class:`NewsTelegramBot.start_scheduler` - lên lịch crawl định kỳ bằng
  JobQueue của python-telegram-bot (một process, một event loop).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta

from telegram import Bot, Update
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from app.config import Settings
from app.database.models import Category, CrawlStats, NewsRecord
from app.database.repository import NewsRepository
from app.dedupe.normalizer import utcnow
from app.logging_config import get_logger
from app.telegram.formatter import (
    TELEGRAM_MAX_LENGTH,
    format_crawl_result,
    format_latest,
    format_status,
    format_trending,
    split_message,
)

logger = get_logger(__name__)

CrawlCallable = Callable[[str], Awaitable[CrawlStats]]

RETRYABLE_ERRORS: tuple[type[TelegramError], ...] = (TimedOut, NetworkError)
AUTO_CRAWL_SETTING = "auto_crawl_enabled"


class TelegramNotifier:
    """Gửi tin qua Telegram, quản lý cờ ``is_reported``."""

    def __init__(
        self,
        bot: Bot,
        chat_id: str,
        settings: Settings,
        repository: NewsRepository,
    ) -> None:
        self._bot = bot
        self._chat_id = str(chat_id)
        self._settings = settings
        self._repo = repository
        self._lock = asyncio.Lock()
        self._inflight: set[int] = set()

    @property
    def chat_id(self) -> str:
        return self._chat_id

    async def shutdown(self) -> None:
        """Đóng phiên HTTP của bot."""
        try:
            await self._bot.shutdown()
        except Exception as exc:  # noqa: BLE001 - đã đóng rồi thì bỏ qua
            logger.debug("Lỗi khi shutdown bot: %s", exc)

    # ------------------------------------------------------------------
    # Gửi thô
    # ------------------------------------------------------------------

    async def _send_with_retry(
        self,
        text: str,
        reply_to: int | None = None,
        chat_id: str | None = None,
    ) -> int | None:
        """Gửi 1 message (tự chia nhỏ nếu quá 4096 ký tự). Trả về message_id hoặc None."""
        target_chat_id = chat_id or self._chat_id
        chunks = split_message(text, TELEGRAM_MAX_LENGTH)
        first_message_id: int | None = None
        for index, chunk in enumerate(chunks):
            message_id = await self._send_chunk(
                chunk,
                reply_to if index == 0 else None,
                target_chat_id,
            )
            if message_id is None:
                # Không đánh dấu cả nhóm đã gửi nếu Telegram chỉ nhận được một
                # phần. Vòng sau sẽ thử lại thay vì làm mất các chunk còn lại.
                return None
            if first_message_id is None:
                first_message_id = message_id
        return first_message_id

    async def _send_chunk(self, text: str, reply_to: int | None, chat_id: str) -> int | None:
        """Gửi một chunk có retry + backoff. Không raise, chỉ log."""
        attempts = self._settings.telegram_max_retries
        for attempt in range(1, attempts + 1):
            try:
                message = await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=None,
                    disable_web_page_preview=False,
                    reply_to_message_id=reply_to,
                )
                return int(message.message_id)
            except RetryAfter as exc:
                wait = float(getattr(exc, "retry_after", self._settings.telegram_retry_base_delay))
                logger.warning("Telegram yêu cầu chờ %.1fs trước khi gửi lại", wait)
                await asyncio.sleep(wait)
            except Forbidden as exc:
                logger.error(
                    "Telegram từ chối gửi tới chat %s (bot bị chặn hoặc sai chat id): %s",
                    chat_id, exc,
                )
                return None
            except BadRequest as exc:
                logger.error("Telegram từ chối nội dung message: %s", exc)
                return None
            except RETRYABLE_ERRORS as exc:
                if attempt >= attempts:
                    break
                delay = self._settings.telegram_retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Lỗi mạng Telegram (lần %d/%d), thử lại sau %.1fs: %s",
                    attempt, attempts, delay, exc,
                )
                await asyncio.sleep(delay)
            except TelegramError as exc:
                if attempt >= attempts:
                    break
                delay = self._settings.telegram_retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Lỗi Telegram (lần %d/%d), thử lại sau %.1fs: %s",
                    attempt, attempts, delay, exc,
                )
                await asyncio.sleep(delay)
            except Exception as exc:  # noqa: BLE001
                # Một lỗi lạ (timeout socket, lỗi driver, bug nội bộ) không được
                # làm hỏng cả lô tin AI + CRYPTO của vòng gửi này.
                logger.exception(
                    "Lỗi không mong đợi khi gửi Telegram (lần %d/%d): %s",
                    attempt, attempts, exc,
                )
                if attempt >= attempts:
                    break
                delay = self._settings.telegram_retry_base_delay * (2 ** (attempt - 1))
                await asyncio.sleep(delay)
        logger.error("Gửi Telegram thất bại sau %d lần thử", attempts)
        return None

    async def send_text(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to: int | None = None,
    ) -> bool:
        """Gửi text thuần (dùng cho phản hồi lệnh)."""
        if self._settings.telegram_rate_limit_seconds > 0:
            await asyncio.sleep(self._settings.telegram_rate_limit_seconds)
        return await self._send_with_retry(text, reply_to=reply_to, chat_id=chat_id) is not None

    # ------------------------------------------------------------------
    # Gửi tin nổi bật
    # ------------------------------------------------------------------

    async def send_trending(self, records: Sequence[NewsRecord], now: datetime | None = None) -> tuple[int, int]:
        """Gửi danh sách tin trending, đánh dấu đã gửi.

        Trả về ``(số tin gửi thành công, số tin gửi lỗi)``. Tin nào gửi lỗi
        sẽ **không** được đánh dấu nên vòng sau sẽ gửi lại.
        """
        if not records:
            return 0, 0
        reference = now or utcnow()

        async with self._lock:
            pending = await self._filter_unsent(records)
            if not pending:
                logger.info("Không có tin mới để gửi (tất cả đã báo trước đó)")
                return 0, 0

            sent = 0
            failed = 0
            for category in (Category.AI, Category.CRYPTO):
                group = [record for record in pending if record.category is category]
                if not group:
                    continue
                text = format_trending(category, group, reference)
                message_id = await self._send_with_retry(text)
                if message_id is None:
                    failed += len(group)
                    for record in group:
                        await self._mark_failed(record, "gửi telegram thất bại")
                    continue

                for record in group:
                    if await self._mark_reported(record, message_id):
                        sent += 1
                    else:
                        # Đã được đánh dấu bởi lần gửi khác (race) -> coi như đã gửi
                        sent += 1
                if self._settings.telegram_rate_limit_seconds > 0:
                    await asyncio.sleep(self._settings.telegram_rate_limit_seconds)
            return sent, failed

    async def _filter_unsent(self, records: Sequence[NewsRecord]) -> list[NewsRecord]:
        """Loại tin đã gửi trước đó (kiểm tra DB + danh sách đang gửi trong RAM)."""
        pending: list[NewsRecord] = []
        for record in records:
            if record.is_reported or record.id in self._inflight:
                continue
            if await self._repo.is_reported(record.id):
                continue
            pending.append(record)
            self._inflight.add(record.id)
        return pending

    async def _mark_reported(self, record: NewsRecord, message_id: int) -> bool:
        try:
            marked = await self._repo.mark_reported(record.id, message_id, self._chat_id)
            if not marked:
                logger.info("Tin #%s đã được đánh dấu gửi trước đó, bỏ qua", record.id)
            return marked
        except Exception as exc:  # noqa: BLE001 - lỗi DB phải log rõ
            logger.error("Không đánh dấu tin #%s là đã gửi: %s", record.id, exc)
            return False
        finally:
            self._inflight.discard(record.id)

    async def _mark_failed(self, record: NewsRecord, error: str) -> None:
        self._inflight.discard(record.id)
        try:
            await self._repo.mark_report_failed(record.id, self._chat_id, error)
        except Exception as exc:  # noqa: BLE001
            logger.error("Không ghi log lỗi gửi cho tin #%s: %s", record.id, exc)


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------

HELP_TEXT = """🤖 AI & CRYPTO NEWS BOT

Lệnh có sẵn:
/latest - 10 tin mới nhất (AI + Crypto)
/ai - tin AI mới nhất
/crypto - tin Crypto mới nhất
/trending - top tin đang nổi bật
/status - trạng thái crawler và database
/crawl - chạy crawl thủ công ngay bây giờ
/auto on|off|status - bật, tắt hoặc xem trạng thái crawl tự động
/help - danh sách lệnh

Tin tự động được gửi tối đa một lần mỗi bài."""

WELCOME_TEXT = """👋 Chào bạn! Tôi theo dõi tin AI và Crypto và gửi tin nổi bật.

Gõ /help để xem danh sách lệnh."""


class NewsTelegramBot:
    """Đóng gói Telegram Application, các lệnh và lịch crawl định kỳ."""

    def __init__(
        self,
        settings: Settings,
        repository: NewsRepository,
        run_crawl: CrawlCallable,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repository
        self._run_crawl = run_crawl
        self._crawl_lock = asyncio.Lock()
        self._auto_lock = asyncio.Lock()
        self._application: Application | None = None
        self._notifier = notifier
        self._auto_crawl_enabled: bool | None = None

    # ------------------------------------------------------------------
    # Khởi tạo
    # ------------------------------------------------------------------

    def build(self) -> Application:
        """Tạo Application của python-telegram-bot và đăng ký handler."""
        token = self._settings.telegram_bot_token.strip()
        if not token:
            raise ValueError("Thiếu TELEGRAM_BOT_TOKEN trong .env")

        application = (
            ApplicationBuilder()
            .token(token)
            .post_init(self.on_startup)
            .post_shutdown(self.on_shutdown)
            .build()
        )
        application.add_handler(CommandHandler("start", self._cmd_start))
        application.add_handler(CommandHandler("help", self._cmd_help))
        application.add_handler(CommandHandler("latest", self._cmd_latest))
        application.add_handler(CommandHandler("ai", self._cmd_ai))
        application.add_handler(CommandHandler("crypto", self._cmd_crypto))
        application.add_handler(CommandHandler("trending", self._cmd_trending))
        application.add_handler(CommandHandler("status", self._cmd_status))
        application.add_handler(CommandHandler("crawl", self._cmd_crawl))
        application.add_handler(CommandHandler("auto", self._cmd_auto))
        application.add_error_handler(self._on_error)

        self._application = application
        logger.info("Đã đăng ký %d lệnh Telegram", 9)
        return application

    @property
    def notifier(self) -> TelegramNotifier:
        if self._notifier is None:
            if self._application is None:
                raise RuntimeError("Chưa build() Application")
            self._notifier = TelegramNotifier(
                bot=self._application.bot,
                chat_id=self._settings.telegram_chat_id.strip(),
                settings=self._settings,
                repository=self._repo,
            )
        return self._notifier

    # ------------------------------------------------------------------
    # Vòng đời + scheduler
    # ------------------------------------------------------------------

    async def on_startup(self, _application: Application) -> None:
        """Khôi phục lịch tự động nhưng không crawl ngay khi process khởi động."""
        logger.info("Telegram bot đã khởi động (chat id: %s)", self._settings.telegram_chat_id)
        enabled = await self._get_auto_crawl_enabled()
        if enabled:
            self.start_scheduler()
        state = "bật" if enabled else "tắt"
        await self._announce(
            f"🚀 Bot đã khởi động. Crawl tự động: {state}. "
            f"Dùng /crawl để cập nhật ngay hoặc /auto status để xem lịch."
        )

    async def on_shutdown(self, _application: Application) -> None:
        logger.info("Telegram bot đang tắt")

    def start_scheduler(self) -> None:
        """Đăng ký job crawl định kỳ trên JobQueue của PTB."""
        if self._application is None:
            raise RuntimeError("Chưa build() Application")
        if self._application.job_queue is None:
            raise RuntimeError("Thiếu JobQueue; hãy cài python-telegram-bot[job-queue]")
        self.stop_scheduler()
        interval = timedelta(minutes=self._settings.crawl_interval_minutes)
        self._application.job_queue.run_repeating(
            self._scheduled_job,
            interval=interval,
            first=interval,
            name="crawl-job",
            job_kwargs={"coalesce": True},
        )
        logger.info("Đã lên lịch crawl %s", self._interval_text())

    def stop_scheduler(self) -> int:
        """Hủy mọi job crawl đang đăng ký và trả số job đã hủy."""
        if self._application is None or self._application.job_queue is None:
            return 0
        jobs = self._application.job_queue.get_jobs_by_name("crawl-job")
        for job in jobs:
            job.schedule_removal()
        if jobs:
            logger.info("Đã hủy %d lịch crawl tự động", len(jobs))
        return len(jobs)

    def _interval_text(self) -> str:
        minutes = self._settings.crawl_interval_minutes
        if minutes % 1440 == 0:
            return f"mỗi {minutes // 1440} ngày"
        if minutes % 60 == 0:
            return f"mỗi {minutes // 60} giờ"
        return f"mỗi {minutes} phút"

    async def _get_auto_crawl_enabled(self) -> bool:
        if self._auto_crawl_enabled is not None:
            return self._auto_crawl_enabled
        raw = await self._repo.get_app_setting(AUTO_CRAWL_SETTING)
        if raw is None:
            enabled = self._settings.auto_crawl_enabled
            await self._repo.set_app_setting(AUTO_CRAWL_SETTING, "1" if enabled else "0")
        else:
            enabled = raw.strip().lower() in {"1", "true", "yes", "on"}
        self._auto_crawl_enabled = enabled
        return enabled

    async def _set_auto_crawl_enabled(self, enabled: bool) -> None:
        async with self._auto_lock:
            await self._repo.set_app_setting(AUTO_CRAWL_SETTING, "1" if enabled else "0")
            self._auto_crawl_enabled = enabled
            if enabled:
                self.start_scheduler()
            else:
                self.stop_scheduler()

    async def _scheduled_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._get_auto_crawl_enabled():
            logger.info("Bỏ qua job scheduler vì crawl tự động đang tắt")
            return
        await self._run_scheduled_crawl("scheduler")

    async def _run_scheduled_crawl(self, reason: str) -> None:
        """Chạy crawl với khoá để không bao giờ chạy chồng nhau."""
        if self._crawl_lock.locked():
            logger.warning("Bỏ qua crawl (%s): vòng trước còn đang chạy", reason)
            return
        async with self._crawl_lock:
            try:
                await self._run_crawl(reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - lỗi crawl không được làm sập bot
                logger.exception("Crawl (%s) thất bại: %s", reason, exc)

    # ------------------------------------------------------------------
    # Tiện ích
    # ------------------------------------------------------------------

    def _is_allowed(self, update: Update) -> bool:
        allowed = self._settings.allowed_user_ids
        if not allowed:
            return True
        user = update.effective_user
        return bool(user and str(user.id) in allowed)

    async def _announce(self, text: str) -> None:
        try:
            await self.notifier.send_text(text)
        except Exception as exc:  # noqa: BLE001 - thông báo là tuỳ chọn
            logger.warning("Không gửi được thông báo khởi động: %s", exc)

    async def _reply(self, update: Update, text: str) -> None:
        if not self._is_allowed(update):
            logger.warning("Từ chối lệnh từ user %s", update.effective_user.id if update.effective_user else "?")
            return
        chat = update.effective_chat
        if chat is None:
            logger.warning("Không phản hồi được update không có effective_chat")
            return
        message = update.effective_message
        try:
            await self.notifier.send_text(
                text,
                chat_id=str(chat.id),
                reply_to=message.message_id if message is not None else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Không gửi được phản hồi: %s", exc)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Lỗi khi xử lý update: %s", context.error, exc_info=context.error)

    # ------------------------------------------------------------------
    # Lệnh
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, WELCOME_TEXT)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, HELP_TEXT)

    async def _cmd_latest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        records = await self._repo.select_latest(limit=10)
        await self._reply(update, format_latest(records, utcnow()))

    async def _cmd_ai(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        records = await self._repo.select_latest(limit=10, category=Category.AI)
        await self._reply(update, format_latest(records, utcnow(), title="🤖 TIN AI MỚI NHẤT"))

    async def _cmd_crypto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        records = await self._repo.select_latest(limit=10, category=Category.CRYPTO)
        await self._reply(update, format_latest(records, utcnow(), title="💰 TIN CRYPTO MỚI NHẤT"))

    async def _cmd_trending(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        settings = self._settings
        records: list[NewsRecord] = []
        for category in (Category.AI, Category.CRYPTO):
            records.extend(
                await self._repo.select_trending(
                    category=category,
                    min_score=0.0,  # lệnh /trending hiển thị cả tin chưa đạt ngưỡng tự động
                    max_age_hours=settings.trend_max_age_hours,
                    limit=settings.trend_max_per_category,
                    only_unreported=False,
                )
            )
        await self._reply(update, format_trending(None, records, utcnow()))

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        stats = await self._repo.get_stats()
        auto_enabled = await self._get_auto_crawl_enabled()
        summary = {
            "Browser": f"{self._settings.browser_engine.value} (headless={self._settings.browser_headless})",
            "Crawl tự động": f"{'bật' if auto_enabled else 'tắt'} ({self._interval_text()})",
            "Ngưỡng trending": f"{self._settings.trend_min_score:.2f}",
            "Tin tối đa / chủ đề": self._settings.trend_max_per_category,
        }
        await self._reply(update, format_status(stats, summary, utcnow()))

    async def _cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            logger.warning("Từ chối lệnh /auto từ user không được phép")
            return
        action = context.args[0].strip().lower() if context.args else "status"
        if action == "on":
            await self._set_auto_crawl_enabled(True)
            await self._reply(
                update,
                f"✅ Crawl tự động đã bật ({self._interval_text()}). "
                "Bot không crawl ngay; dùng /crawl nếu muốn cập nhật bây giờ.",
            )
            return
        if action == "off":
            await self._set_auto_crawl_enabled(False)
            await self._reply(update, "⏸ Crawl tự động đã tắt. Lệnh /crawl vẫn hoạt động.")
            return
        if action == "status":
            enabled = await self._get_auto_crawl_enabled()
            await self._reply(
                update,
                f"⚙️ Crawl tự động: {'bật' if enabled else 'tắt'} ({self._interval_text()}).",
            )
            return
        await self._reply(update, "Cú pháp: /auto on, /auto off hoặc /auto status")

    async def _cmd_crawl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            logger.warning("Từ chối lệnh /crawl từ user không được phép")
            return
        if self._crawl_lock.locked():
            await self._reply(update, "⏳ Đang có một vòng crawl chạy, vui lòng đợi hoàn tất.")
            return

        application = context.application

        async def _run() -> None:
            async with self._crawl_lock:
                started = utcnow()
                try:
                    stats = await self._run_crawl("manual")
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Crawl thủ công lỗi: %s", exc)
                    await self._announce(f"❌ Crawl thất bại: {exc}")
                    return
                duration = (utcnow() - started).total_seconds()
                await self._announce(format_crawl_result(stats.as_dict(), duration))

        await self._reply(update, "🔄 Bắt đầu crawl thủ công…")
        application.create_task(_run(), name="manual-crawl")


__all__ = [
    "CrawlCallable",
    "HELP_TEXT",
    "NewsTelegramBot",
    "TelegramNotifier",
]
