"""Điểm vào của ứng dụng: pipeline crawl → dedup → SQLite → trend → Telegram.

Luồng chính::

    Scheduler / /crawl
        ↓
    Crawler (Playwright + Camoufox)
        ↓
    Parse + Normalize
        ↓
    Deduplicate
        ↓
    Save SQLite
        ↓
    Calculate trend score
        ↓
    Select trending news
        ↓
    Telegram  (chỉ tin chưa từng report)
        ↓
    Mark is_reported = true

Các lệnh CLI::

    python -m app.main                 # bot Telegram + scheduler
    python -m app.main once            # crawl một vòng rồi thoát
    python -m app.main once --dry-run  # không gửi Telegram
    python -m app.main probe           # xuất thông tin fingerprint browser
    python -m app.main init-db         # chỉ tạo database
    python -m app.main stats           # in thống kê
    python -m app.main skip-existing   # bỏ qua tin cũ trước khi bật Telegram
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from types import TracebackType

from telegram import Bot
from telegram.error import TelegramError

from app.config import BrowserEngine, Settings, get_settings
from app.crawler.browser import BrowserManager
from app.crawler.sources import SourceCrawler, create_sources
from app.database.models import Category, CrawlStats, NewsItem, NewsRecord
from app.database.repository import NewsRepository
from app.dedupe.deduplicator import Deduplicator, MatchKind
from app.dedupe.normalizer import hours_since, overlap_coefficient, token_set, utcnow
from app.logging_config import get_logger, setup_logging
from app.telegram.bot import NewsTelegramBot, TelegramNotifier
from app.telegram.formatter import format_status
from app.trend.analyzer import TrendAnalyzer

logger = get_logger("app.main")

TOPIC_TITLE_OVERLAP = 0.4


class Pipeline:
    """Chạy trọn một vòng: crawl → dedup → lưu → trend → Telegram."""

    def __init__(
        self,
        settings: Settings,
        repository: NewsRepository,
        browser: BrowserManager,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.browser = browser
        self.notifier = notifier
        self.analyzer = TrendAnalyzer(settings)
        self.deduplicator = Deduplicator(repository, settings)
        self.crawler = SourceCrawler(settings, browser)

    # ------------------------------------------------------------------
    # Vòng crawl
    # ------------------------------------------------------------------

    async def run_once(
        self,
        reason: str = "manual",
        category: Category | None = None,
        only: list[str] | None = None,
    ) -> CrawlStats:
        """Chạy một vòng crawl đầy đủ và trả về thống kê."""
        started_wall = time.monotonic()
        now = utcnow()
        stats = CrawlStats(started_at=now)
        stats.run_id = await self.repository.start_run(now)
        logger.info("=== Bắt đầu vòng crawl (%s), run_id=%s ===", reason, stats.run_id)

        try:
            sources, _ = create_sources(self.settings, category=category, only=only)
            stats.sources_total = len(sources)

            try:
                await self.browser.ensure_alive()
                outcome = await self.crawler.crawl_sources(sources, run_id=str(stats.run_id))
            except Exception as exc:  # noqa: BLE001 - browser hỏng không được làm sập tiến trình
                logger.exception("Không crawl được: %s", exc)
                stats.errors.append(f"crawl: {exc}")
                return stats

            stats.articles_found = outcome.total_articles
            stats.sources_failed = len(outcome.failed_sources)
            stats.sources_ok = stats.sources_total - stats.sources_failed
            for result in outcome.per_source:
                if result.error:
                    stats.errors.append(f"{result.source}: {result.error}")

            new_items, new_ids, linked, linked_ids = await self._deduplicate_and_store(outcome.items, now)
            stats.new_articles = len(new_items)
            stats.duplicates = len(outcome.items) - len(new_items)
            stats.extra_sources_linked = linked

            affected_ids = list(dict.fromkeys([*new_ids, *linked_ids]))
            await self._apply_trend_scores(affected_ids, now)
            await self._broadcast(now, stats)
            return stats
        except asyncio.CancelledError:
            stats.errors.append("pipeline: cancelled")
            raise
        except Exception as exc:
            stats.errors.append(f"pipeline: {type(exc).__name__}: {exc}")
            raise
        finally:
            stats.finished_at = utcnow()
            await self.repository.finish_run(stats)
            self._log_stats(stats, time.monotonic() - started_wall)

    # ------------------------------------------------------------------
    # Các bước
    # ------------------------------------------------------------------

    async def _deduplicate_and_store(
        self,
        items: list[NewsItem],
        now: datetime,
    ) -> tuple[list[NewsItem], list[int], int, list[int]]:
        """Chống trùng rồi ghi vào SQLite.

        Trả về ``(tin mới, id tin mới, số URL liên kết thêm, id tin được liên kết)``.
        """
        if not items:
            return [], [], 0, []

        await self.deduplicator.load_index(now)
        decisions = await self.deduplicator.classify_many(items)

        new_items: list[NewsItem] = []
        for decision in decisions:
            if not decision.is_duplicate:
                new_items.append(decision.item)

        inserted = await self.repository.insert_items(new_items, now=now)
        # Sau resolve_ids, mọi news_id trong decisions đã là id thật của database,
        # kể cả các bài mới (trước insert chúng mang id tạm âm).
        decisions = self.deduplicator.resolve_ids(decisions, inserted)

        linked = 0
        linked_ids: list[int] = []
        for decision in decisions:
            if not decision.is_duplicate or decision.news_id is None or decision.news_id <= 0:
                continue
            try:
                if await self.repository.link_source(decision.news_id, decision.item, now=now):
                    linked += 1
                    linked_ids.append(decision.news_id)
                    logger.info(
                        "Trùng tin (%s%s): gắn nguồn %s vào bản tin #%d",
                        decision.kind.value,
                        f" {decision.similarity:.0%}" if decision.kind is MatchKind.FUZZY else "",
                        decision.item.source,
                        decision.news_id,
                    )
            except Exception as exc:  # noqa: BLE001 - lỗi 1 dòng không làm hỏng cả vòng
                logger.error(
                    "Không gắn được nguồn %s vào tin #%d: %s",
                    decision.item.source,
                    decision.news_id,
                    exc,
                )

        inserted_items = [item for _, item in inserted]
        return inserted_items, [news_id for news_id, _ in inserted], linked, linked_ids

    async def _apply_trend_scores(
        self,
        news_ids: Sequence[int],
        now: datetime,
    ) -> None:
        """Tính lại trend_score cho tin mới và tin vừa được gắn thêm nguồn."""
        if not news_ids:
            return
        affected = await self.repository.get_records(news_ids)
        recent = await self.repository.select_recent(
            self.settings.trend_topic_window_minutes,
            now=now,
        )
        by_id = {record.id: record for record in recent}
        by_id.update({record.id: record for record in affected})
        records = list(by_id.values())
        scores: dict[int, float] = {}
        for record in records:
            components = self.analyzer.score_record(
                record,
                now,
                topic_count=self._topic_count(record, recent, now),
            )
            scores[record.id] = components.total
        await self.repository.update_trend_scores(scores)
        multi = sum(1 for record in records if record.source_count > 1)
        logger.info("Đã tính lại trend score cho %d tin liên quan", len(scores))
        if multi:
            logger.info("Trong đó %d tin được cộng điểm đa nguồn", multi)

    def _topic_count(self, record: NewsRecord, records: list[NewsRecord], now: datetime) -> int:
        """Đếm bài gần thời gian và có tiêu đề chồng lấn đủ để coi là cùng chủ đề."""
        window = self.settings.trend_topic_window_minutes / 60.0
        tokens = token_set(record.title)
        count = 0
        for other in records:
            if other.category is not record.category:
                continue
            timestamp = other.published_at or other.first_seen_at
            if hours_since(timestamp, now) > window:
                continue
            if other.id == record.id or overlap_coefficient(tokens, token_set(other.title)) >= TOPIC_TITLE_OVERLAP:
                count += 1
        return max(1, count)

    async def _broadcast(self, now: datetime, stats: CrawlStats) -> None:
        """Chọn tin trending rồi gửi Telegram (nếu có notifier)."""
        if self.notifier is None:
            logger.info("Dry-run: bỏ qua bước gửi Telegram")
            return

        selected: list[NewsRecord] = []
        counts: dict[Category, int] = {Category.AI: 0, Category.CRYPTO: 0}
        for category in Category:
            records = await self.repository.select_trending(
                category=category,
                min_score=self.settings.trend_min_score,
                max_age_hours=self.settings.trend_max_age_hours,
                limit=self.settings.trend_max_per_category,
                now=now,
                only_unreported=True,
            )
            selected.extend(records)
            counts[category] = len(records)

        logger.info(
            "Tin trending AI: %d | CRYPTO: %d (ngưỡng %.2f)",
            counts[Category.AI], counts[Category.CRYPTO], self.settings.trend_min_score,
        )
        if not selected:
            logger.info("Không có tin nào đạt ngưỡng trending")
            return

        try:
            sent, failed = await self.notifier.send_trending(selected, now)
            stats.telegram_sent = sent
            stats.telegram_failed = failed
            if failed:
                stats.errors.append(f"telegram: {failed} tin gửi lỗi, vòng sau sẽ gửi lại")
        except Exception as exc:  # noqa: BLE001 - lỗi Telegram không được làm mất dữ liệu
            logger.exception("Lỗi khi gửi Telegram: %s", exc)
            stats.telegram_failed = len(selected)
            stats.errors.append(f"telegram: {exc}")

    @staticmethod
    def _log_stats(stats: CrawlStats, duration: float) -> None:
        logger.info("Kết quả vòng crawl: %s", stats.as_dict())
        logger.info(
            "Tổng kết: tìm thấy %d | mới %d | trùng %d | Telegram gửi %d (lỗi %d) | %.1fs",
            stats.articles_found,
            stats.new_articles,
            stats.duplicates,
            stats.telegram_sent,
            stats.telegram_failed,
            duration,
        )


# --------------------------------------------------------------------------
# Khởi tạo
# --------------------------------------------------------------------------


async def build_repository(settings: Settings) -> NewsRepository:
    """Mở database, chạy migration và trả về repository."""
    from app.database.database import Database

    database = Database(settings.resolved_database_path)
    repository = NewsRepository(database)
    await repository.initialize()
    logger.info("Database sẵn sàng: %s", settings.resolved_database_path)
    return repository


async def build_notifier(settings: Settings, repository: NewsRepository) -> TelegramNotifier | None:
    """Tạo TelegramNotifier nếu đã cấu hình đủ token + chat id."""
    if not settings.telegram_ready:
        return None
    bot = Bot(token=settings.telegram_bot_token.strip())
    await bot.initialize()
    me = await bot.get_me()
    logger.info("Đã kết nối Telegram bot: @%s (id=%s)", me.username, me.id)
    return TelegramNotifier(bot, settings.telegram_chat_id.strip(), settings, repository)


# --------------------------------------------------------------------------
# Các lệnh CLI
# --------------------------------------------------------------------------


async def cmd_once(settings: Settings, args: argparse.Namespace) -> int:
    """Chạy một vòng crawl rồi thoát."""
    repository = await build_repository(settings)
    browser = BrowserManager(settings)
    notifier: TelegramNotifier | None = None

    if args.dry_run or not settings.telegram_ready:
        if not args.dry_run:
            logger.warning(
                "Chưa cấu hình TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID trong .env -> chạy dry-run, không gửi tin"
            )
        notifier = None
    else:
        try:
            notifier = await build_notifier(settings, repository)
        except TelegramError as exc:
            logger.error("Không kết nối được Telegram bot: %s", exc)
            await repository.close()
            return 2

    category = Category(args.category.upper()) if args.category else None
    only = [name for name in (args.sources or "").split(",") if name.strip()]
    pipeline = Pipeline(settings, repository, browser, notifier)

    exit_code = 0
    try:
        await browser.start()
        stats = await pipeline.run_once(reason=args.reason, category=category, only=only or None)
        exit_code = 0 if stats.sources_failed == 0 else 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Vòng crawl lỗi: %s", exc)
        exit_code = 3
    finally:
        await browser.stop()
        if notifier is not None:
            await notifier.shutdown()
        print(format_status(await repository.get_stats(), {}, datetime.now().astimezone()))
        await repository.close()
    return exit_code


async def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    """Chạy Telegram bot kèm scheduler trong một process duy nhất."""
    if not settings.telegram_ready:
        logger.error(
            "Chế độ bot cần TELEGRAM_BOT_TOKEN và TELEGRAM_CHAT_ID trong .env. "
            "Hãy điền hoặc dùng `python -m app.main once --dry-run`."
        )
        return 2

    repository = await build_repository(settings)
    browser = BrowserManager(settings)
    try:
        notifier = await build_notifier(settings, repository)
    except TelegramError as exc:
        logger.error("Không kết nối được Telegram bot: %s", exc)
        await repository.close()
        return 2

    assert notifier is not None
    pipeline = Pipeline(settings, repository, browser, notifier)
    bot = NewsTelegramBot(settings, repository, pipeline.run_once, notifier=notifier)
    application = bot.build()

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    try:
        await application.initialize()
        await application.start()
        if application.updater is not None:
            await application.updater.start_polling(drop_pending_updates=True)
        await bot.on_startup(application)
        logger.info("Đã sẵn sàng. Nhấn Ctrl+C để dừng.")
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.info("Ứng dụng bị hủy")
    finally:
        logger.info("Đang tắt ứng dụng...")
        if application.updater is not None:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await browser.stop()
        await notifier.shutdown()
        await repository.close()
    return 0


def _install_signal_handlers(
    stop_event: asyncio.Event,
) -> None:
    """Cho phép Ctrl+C dừng ứng dụng gọn gàng (Windows chỉ hỗ trợ SIGINT/SIGTERM)."""

    def _handler(_signum: int, _frame: TracebackType | None) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError, ValueError, RuntimeError):
            signal.signal(sig, _handler)


async def cmd_probe(settings: Settings, args: argparse.Namespace) -> int:
    """Thu thập và in thông tin fingerprint của browser (mục đích nghiên cứu)."""
    engines: list[BrowserEngine] = [BrowserEngine(args.engine)] if args.engine else list(BrowserEngine)
    for engine in engines:
        probe_settings = settings.model_copy(update={"browser_engine": engine})
        browser = BrowserManager(probe_settings)
        snapshot: dict[str, object] | None = None
        saved: object | None = None
        try:
            await browser.start()
            snapshot = await browser.fingerprint_snapshot()
            saved = await browser.save_fingerprint_report()
        except Exception as exc:  # noqa: BLE001 - engine chưa cài thì báo và bỏ qua
            logger.error("Không probe được engine %s: %s", engine.value, exc)
        finally:
            await browser.stop()
        if snapshot is None:
            continue

        print(f"\n=== FINGERPRINT: {engine.value} ===")
        for key in (
            "browser_version", "user_agent", "platform", "language", "languages",
            "hardware_concurrency", "device_memory", "max_touch_points", "webdriver",
            "timezone", "screen", "viewport", "webgl_vendor", "webgl_renderer",
            "cookies_enabled", "do_not_track",
        ):
            print(f"  {key:20}: {snapshot.get(key)}")
        print(f"  {'saved_to':20}: {saved}")
    return 0


async def cmd_init_db(settings: Settings, args: argparse.Namespace) -> int:
    repository = await build_repository(settings)
    await repository.close()
    print(f"Đã khởi tạo database: {settings.resolved_database_path}")
    return 0


async def cmd_stats(settings: Settings, args: argparse.Namespace) -> int:
    repository = await build_repository(settings)
    try:
        print(format_status(await repository.get_stats(), {}, datetime.now().astimezone()))
    finally:
        await repository.close()
    return 0


async def cmd_skip_existing(settings: Settings, args: argparse.Namespace) -> int:
    """Bỏ qua toàn bộ tin hiện có để Telegram chỉ gửi tin phát sinh về sau."""
    repository = await build_repository(settings)
    try:
        count = await repository.count_unhandled()
        if args.dry_run:
            print(f"Sẽ đánh dấu bỏ qua {count} tin hiện có (database chưa thay đổi).")
            return 0
        skipped = await repository.skip_unhandled()
        print(f"Đã đánh dấu bỏ qua {skipped} tin hiện có. Telegram sẽ chỉ gửi tin mới.")
        return 0
    finally:
        await repository.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="AI & Crypto News Crawler - Playwright + Camoufox + SQLite + Telegram",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="chạy Telegram bot + scheduler (mặc định)")
    serve.set_defaults(func=cmd_serve)

    once = sub.add_parser("once", help="chạy một vòng crawl rồi thoát")
    once.add_argument("--dry-run", action="store_true", help="không gửi Telegram")
    once.add_argument("--category", choices=["ai", "crypto"], help="chỉ crawl một chủ đề")
    once.add_argument("--sources", help="chỉ crawl các nguồn được chỉ định, cách nhau bởi dấu phẩy")
    once.add_argument("--reason", default="cli", help="nhãn ghi trong log")
    once.set_defaults(func=cmd_once, dry_run=False)

    probe = sub.add_parser("probe", help="xuất thông tin fingerprint của browser")
    probe.add_argument("--engine", choices=[engine.value for engine in BrowserEngine])
    probe.set_defaults(func=cmd_probe)

    init_db = sub.add_parser("init-db", help="tạo database và chạy migration")
    init_db.set_defaults(func=cmd_init_db)

    stats = sub.add_parser("stats", help="in thống kê database")
    stats.set_defaults(func=cmd_stats)

    skip_existing = sub.add_parser(
        "skip-existing",
        help="bỏ qua tin hiện có để Telegram chỉ gửi tin phát sinh về sau",
    )
    skip_existing.add_argument("--dry-run", action="store_true", help="chỉ in số tin, không thay đổi database")
    skip_existing.set_defaults(func=cmd_skip_existing, dry_run=False)

    return parser


async def async_main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    setup_logging(settings)

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        args = parser.parse_args(["serve"])

    logger.info(
        "Khởi động | engine=%s headless=%s | db=%s | telegram=%s",
        settings.browser_engine.value,
        settings.browser_headless,
        settings.resolved_database_path,
        "bật" if settings.telegram_ready else "tắt",
    )
    return int(await args.func(settings, args))


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Đã dừng theo yêu cầu (Ctrl+C)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
