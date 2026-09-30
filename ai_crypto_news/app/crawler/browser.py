"""BrowserManager: quản lý vòng đời browser cho crawler.

Hỗ trợ hai engine qua cùng một interface:

* ``chromium`` - Playwright Chromium, ổn định cho crawl thông thường.
* ``camoufox`` - Firefox đã sửa fingerprint, dùng để **nghiên cứu và so sánh
  hành vi browser/fingerprint**. Camoufox không dùng để né CAPTCHA hay vượt
  cơ chế bảo vệ của website; mọi nguồn tin trong project đều công khai và
  project vẫn tôn trọng robots.txt.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from app.config import BrowserEngine, Settings
from app.crawler.base import FINGERPRINT_JS, block_heavy_resources
from app.logging_config import get_logger

logger = get_logger(__name__)

CHROMIUM_ARGS: tuple[str, ...] = (
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
)


@dataclass(slots=True)
class TraceHandle:
    """Thông tin trace của một context, dùng để lưu file zip khi đóng."""

    context: BrowserContext
    path: Path
    started: bool = False


class BrowserUnavailableError(RuntimeError):
    """Browser không khởi động được hoặc đã crash."""


class BrowserManager:
    """Khởi động, khởi động lại và cấp phát browser context."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._camoufox_cm: Any = None
        self._lock = asyncio.Lock()
        self._started = False
        self._restart_count = 0

    # ------------------------------------------------------------------
    # Thuộc tính
    # ------------------------------------------------------------------

    @property
    def engine(self) -> BrowserEngine:
        return self.settings.browser_engine

    @property
    def is_running(self) -> bool:
        return self._started and self._browser is not None and self._browser.is_connected()

    @property
    def restart_count(self) -> int:
        return self._restart_count

    def _bot_user_agent(self) -> str:
        """User-Agent định danh bot (dùng cho robots.txt và header)."""
        if self.settings.browser_user_agent:
            return self.settings.browser_user_agent
        return f"AI-Crypto-News-Crawler/1.0 (+https://example.local/bot)"

    # ------------------------------------------------------------------
    # Vòng đời
    # ------------------------------------------------------------------

    async def start(self) -> Browser:
        """Khởi động browser theo engine cấu hình (idempotent)."""
        async with self._lock:
            if self.is_running:
                return self._browser  # type: ignore[return-value]
            if self._playwright is None:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
            try:
                self._browser = await self._launch()
            except Exception as exc:  # noqa: BLE001 - báo lỗi rõ ràng cho user
                await self._teardown()
                raise BrowserUnavailableError(
                    f"Không khởi động được browser engine={self.engine.value}: {exc}"
                ) from exc
            self._started = True
            logger.info(
                "Browser started: engine=%s headless=%s version=%s",
                self.engine.value,
                self.settings.browser_headless,
                self._browser.version,
            )
            return self._browser

    async def _launch(self) -> Browser:
        if self.engine is BrowserEngine.CAMOUFOX:
            return await self._launch_camoufox()
        return await self._launch_chromium()

    async def _launch_chromium(self) -> Browser:
        assert self._playwright is not None
        return await self._playwright.chromium.launch(
            headless=self.settings.browser_headless,
            args=list(CHROMIUM_ARGS),
        )

    async def _launch_camoufox(self) -> Browser:
        """Khởi động Camoufox, tự thích ứng khác biệt tham số giữa các phiên bản."""
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as exc:  # pragma: no cover - phụ thuộc môi trường
            raise BrowserUnavailableError(
                "Chưa cài camoufox. Chạy: pip install camoufox && camoufox fetch"
            ) from exc

        options: dict[str, Any] = {
            "headless": self.settings.camoufox_headless,
            "block_images": self.settings.camoufox_block_images,
            "block_webrtc": self.settings.camoufox_block_webrtc,
            "humanize": self.settings.camoufox_humanize,
            # Camoufox 0.5.x dùng tên `locale`; `i18n` gây TypeError và buộc
            # manager launch lại browser lần hai.
            "locale": self.settings.camoufox_i18n,
        }
        os_list = self.settings.camoufox_os_list
        if os_list:
            options["os"] = os_list[0] if len(os_list) == 1 else os_list
        if self.settings.camoufox_executable_path:
            options["executable_path"] = self.settings.camoufox_executable_path

        self._camoufox_cm = AsyncCamoufox(**options)
        try:
            browser = await self._camoufox_cm.__aenter__()
        except TypeError:
            # Phiên bản camoufox cũ không nhận một số tham số -> thử bản rút gọn
            reduced = {
                key: value
                for key, value in options.items()
                if key in {"headless", "block_images", "block_webrtc", "humanize", "os"}
            }
            logger.warning("Camoufox không nhận %s, thử lại với %s", sorted(options), sorted(reduced))
            self._camoufox_cm = AsyncCamoufox(**reduced)
            browser = await self._camoufox_cm.__aenter__()
        return browser

    async def stop(self) -> None:
        """Đóng browser và giải phóng tài nguyên."""
        async with self._lock:
            await self._teardown()
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
            logger.info("Browser stopped")

    async def _teardown(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001 - browser đã chết sẵn
                logger.debug("Lỗi khi đóng browser: %s", exc)
            self._browser = None
        if self._camoufox_cm is not None:
            try:
                await self._camoufox_cm.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Lỗi khi đóng camoufox: %s", exc)
            self._camoufox_cm = None
        self._started = False

    async def restart(self, reason: str = "") -> Browser:
        """Đóng rồi mở lại browser (dùng khi browser crash)."""
        async with self._lock:
            self._restart_count += 1
            logger.warning("Restart browser lần %d%s", self._restart_count, f" ({reason})" if reason else "")
            await self._teardown()
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
        return await self.start()

    async def ensure_alive(self, reason: str = "") -> Browser:
        """Đảm bảo browser còn sống, tự restart nếu đã crash."""
        if self.is_running:
            assert self._browser is not None
            return self._browser
        return await self.restart(reason or "browser không còn kết nối")

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    async def new_context(self, run_id: str | None = None) -> BrowserContext:
        """Tạo BrowserContext đã cấu hình (UA, viewport, locale, chặn media, trace)."""
        browser = await self.ensure_alive()
        # Chỉ ghi đè User-Agent khi người dùng tự đặt trong .env.
        # Mặc định để Playwright dùng UA thật của browser: không giả danh,
        # và việc tôn trọng robots.txt được kiểm soát ở RobotsGuard, không
        # phụ thuộc vào việc giả UA (UA giả là cách né bot-detection, không dùng).
        context_options: dict[str, Any] = {
            "locale": self.settings.browser_locale,
            "viewport": {
                "width": self.settings.browser_viewport_width,
                "height": self.settings.browser_viewport_height,
            },
            "java_script_enabled": True,
        }
        if self.settings.browser_user_agent:
            context_options["user_agent"] = self.settings.browser_user_agent
        context = await browser.new_context(**context_options)
        context.set_default_timeout(self.settings.browser_timeout_ms)
        context.set_default_navigation_timeout(self.settings.browser_timeout_ms)
        await block_heavy_resources(context, self.settings.block_media)

        if self.settings.trace_enabled and run_id:
            trace_path = self.settings.trace_dir / f"trace_{run_id}.zip"
            handle = TraceHandle(context=context, path=trace_path)
            try:
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                handle.started = True
            except Exception as exc:  # noqa: BLE001 - trace là tuỳ chọn
                logger.warning("Không bật được trace: %s", exc)
            context._trace_handle = handle  # type: ignore[attr-defined]
        return context

    @staticmethod
    async def close_context(context: BrowserContext) -> None:
        """Đóng context và lưu trace nếu có."""
        handle = getattr(context, "_trace_handle", None)
        if isinstance(handle, TraceHandle) and handle.started:
            try:
                handle.path.parent.mkdir(parents=True, exist_ok=True)
                await context.tracing.stop(path=str(handle.path))
                logger.info("Đã lưu trace: %s (mở bằng `playwright show-trace %s`)", handle.path, handle.path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Không lưu được trace %s: %s", handle.path, exc)
        try:
            await context.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lỗi khi đóng context: %s", exc)

    @asynccontextmanager
    async def context_session(self, run_id: str | None = None) -> AsyncIterator[BrowserContext]:
        """Context manager tiện lợi cho mỗi nguồn tin."""
        context = await self.new_context(run_id)
        try:
            yield context
        finally:
            await self.close_context(context)

    # ------------------------------------------------------------------
    # Nghiên cứu fingerprint
    # ------------------------------------------------------------------

    async def fingerprint_snapshot(self, url: str = "https://example.com") -> dict[str, Any]:
        """Thu thập thông tin fingerprint cơ bản của browser đang chạy."""
        async with self.context_session() as context:
            page: Page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.settings.browser_timeout_ms)
                data: dict[str, Any] = await page.evaluate(FINGERPRINT_JS)
            finally:
                await page.close()
        data["engine"] = self.engine.value
        data["browser_version"] = self._browser.version if self._browser else None
        data["collected_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return data

    async def save_fingerprint_report(self, directory: Path | None = None) -> Path:
        """Lưu báo cáo fingerprint ra file JSON để so sánh giữa các engine."""
        snapshot = await self.fingerprint_snapshot()
        target_dir = directory or self.settings.trace_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"fingerprint_{self.engine.value}.json"
        target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Đã lưu báo cáo fingerprint: %s", target)
        return target
