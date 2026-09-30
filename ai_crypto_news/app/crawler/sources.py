"""Registry nguồn tin và runner crawl nhiều nguồn song song bằng asyncio."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.config import Settings
from app.crawler.ai_news import AI_SPECS, create_ai_sources
from app.crawler.base import NewsSource, RobotsGuard, with_retry
from app.crawler.browser import BrowserManager
from app.crawler.crypto_news import CRYPTO_SPECS, create_crypto_sources
from app.database.models import Category, NewsItem, SourceOutcome
from app.logging_config import get_logger

logger = get_logger(__name__)

ALL_SPECS = AI_SPECS + CRYPTO_SPECS


def create_sources(
    settings: Settings,
    category: Category | None = None,
    only: list[str] | None = None,
) -> tuple[list[NewsSource], RobotsGuard]:
    """Khởi tạo các adapter theo category và lọc theo tên nếu cần.

    Nguồn nằm trong ``settings.disabled_sources`` sẽ bị bỏ qua — dùng cho các
    site chặn truy cập tự động (vd. Cloudflare trả 403) mà ta **không** dùng
    kỹ thuật né chặn.
    """
    robots = RobotsGuard(user_agent=settings.browser_user_agent or _bot_ua(), enabled=settings.respect_robots_txt)
    sources: list[NewsSource] = []
    if category in (None, Category.AI):
        sources.extend(create_ai_sources(settings, robots))
    if category in (None, Category.CRYPTO):
        sources.extend(create_crypto_sources(settings, robots))
    if only:
        wanted = {name.strip().lower() for name in only if name.strip()}
        sources = [source for source in sources if source.name.lower() in wanted]
    if settings.enabled_sources:
        enabled = {name.strip().lower() for name in settings.enabled_source_list}
        sources = [source for source in sources if source.name.lower() in enabled]
    if settings.disabled_sources:
        disabled = {name.strip().lower() for name in settings.disabled_source_list}
        kept = [source for source in sources if source.name.lower() not in disabled]
        for source in sources:
            if source.name.lower() in disabled:
                logger.info("Bỏ qua nguồn %s (đang tắt trong DISABLED_SOURCES)", source.name)
        sources = kept
    return sources, robots


def _bot_ua() -> str:
    return "AI-Crypto-News-Crawler/1.0 (+https://example.local/bot)"


@dataclass(slots=True)
class CrawlOutcome:
    """Kết quả crawl của toàn bộ nguồn trong một vòng."""

    items: list[NewsItem] = field(default_factory=list)
    per_source: list[SourceOutcome] = field(default_factory=list)
    total_articles: int = 0
    failed_sources: list[str] = field(default_factory=list)

    def by_category(self, category: Category) -> list[NewsItem]:
        return [item for item in self.items if item.category is category]


class SourceCrawler:
    """Chạy nhiều nguồn song song, mỗi nguồn có timeout + retry riêng."""

    def __init__(self, settings: Settings, browser: BrowserManager) -> None:
        self._settings = settings
        self._browser = browser
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_sources)

    async def crawl_sources(self, sources: list[NewsSource], run_id: str | None = None) -> CrawlOutcome:
        """Crawl toàn bộ nguồn, lỗi 1 nguồn không được làm dừng các nguồn khác."""
        outcome = CrawlOutcome()
        if not sources:
            logger.warning("Không có nguồn tin nào để crawl")
            return outcome

        logger.info("Bắt đầu crawl %d nguồn (tối đa %d đồng thời)", len(sources), self._settings.max_concurrent_sources)
        results = await asyncio.gather(
            *(self._crawl_one(source, run_id) for source in sources),
            return_exceptions=True,
        )

        for source, result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                message = f"{source.name}: {type(result).__name__}: {result}"
                logger.error("Nguồn %s lỗi không xử lý được: %s", source.name, result)
                outcome.failed_sources.append(source.name)
                outcome.per_source.append(
                    SourceOutcome(
                        source=source.name,
                        category=source.category,
                        error=str(result),
                        attempts=self._settings.retry_attempts,
                    )
                )
                continue
            outcome.per_source.append(result)
            outcome.items.extend(result.items)
            outcome.total_articles += len(result.items)
            if result.error:
                outcome.failed_sources.append(result.source)

        logger.info(
            "Crawl xong %d/%d nguồn thành công, tổng %d bài",
            len(sources) - len(outcome.failed_sources),
            len(sources),
            outcome.total_articles,
        )
        return outcome

    async def _crawl_one(self, source: NewsSource, run_id: str | None) -> SourceOutcome:
        """Crawl một nguồn: có retry, timeout và tự restart browser nếu crash."""
        attempts = 0
        last_error: str | None = None
        started = time.monotonic()

        while attempts < self._settings.retry_attempts:
            attempts += 1
            try:
                logger.info("Crawling %s source: %s", source.category.value, source.name)
                async with self._semaphore:
                    items = await self._crawl_with_timeout(source, run_id)
                duration = time.monotonic() - started
                logger.info(
                    "Hoàn tất nguồn %s: tìm thấy %d bài (%.1fs)",
                    source.name, len(items), duration,
                )
                return SourceOutcome(
                    source=source.name,
                    category=source.category,
                    items=items,
                    attempts=attempts,
                    duration_seconds=duration,
                )
            except asyncio.TimeoutError:
                last_error = f"timeout sau {source.spec.source_budget_seconds(self._settings.source_timeout_seconds):.0f}s"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - cô lập lỗi theo từng nguồn
                last_error = f"{type(exc).__name__}: {exc}"
                await self._recover_browser(exc)
            if attempts < self._settings.retry_attempts:
                delay = self._settings.retry_backoff_seconds * attempts
                logger.warning(
                    "Nguồn %s lỗi, thử lại lần %d/%d sau %.1fs: %s",
                    source.name, attempts + 1, self._settings.retry_attempts, delay, last_error,
                )
                await asyncio.sleep(delay)

        duration = time.monotonic() - started
        logger.error(
            "BỎ QUA nguồn %s sau %d lần thử: %s", source.name, attempts, last_error,
        )
        return SourceOutcome(
            source=source.name,
            category=source.category,
            error=last_error or "không rõ nguyên nhân",
            attempts=attempts,
            duration_seconds=duration,
        )

    async def _crawl_with_timeout(self, source: NewsSource, run_id: str | None) -> list[NewsItem]:
        """Giới hạn thời gian cho một nguồn và đóng context dù thành công hay lỗi."""
        budget = source.spec.source_budget_seconds(self._settings.source_timeout_seconds)
        async with asyncio.timeout(budget):
            async with self._browser.context_session(run_id) as context:
                return await source.crawl(context)

    async def _recover_browser(self, error: BaseException) -> None:
        """Nếu lỗi liên quan tới browser đã đóng thì restart ngay."""
        name = type(error).__name__
        if "TargetClosed" in name or "Browser" in name or "closed" in str(error).lower():
            try:
                await self._browser.restart(f"lỗi {name}")
            except Exception as exc:  # noqa: BLE001
                logger.error("Không restart được browser: %s", exc)


async def crawl_all(
    settings: Settings,
    category: Category | None = None,
    only: list[str] | None = None,
    run_id: str | None = None,
) -> tuple[CrawlOutcome, BrowserManager]:
    """Tiện ích một lần: dựng browser, crawl, đóng browser."""
    sources, _ = create_sources(settings, category=category, only=only)
    manager = BrowserManager(settings)
    await manager.start()
    crawler = SourceCrawler(settings, manager)
    try:
        outcome = await crawler.crawl_sources(sources, run_id=run_id)
    finally:
        await manager.stop()
    return outcome, manager


__all__ = [
    "ALL_SPECS",
    "CrawlOutcome",
    "SourceCrawler",
    "crawl_all",
    "create_sources",
    "with_retry",
]
