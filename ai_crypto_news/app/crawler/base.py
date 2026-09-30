"""Kiểm tra robots.txt, định nghĩa source chung và logic crawl danh sách bài.

Không hard-code logic của từng website ở đây: mỗi nguồn chỉ cung cấp một
:class:`SourceSpec` (selector, pattern URL, đường dẫn bị chặn, delay) và
nhận lại một adapter :class:`ListPageSource`.
"""

from __future__ import annotations

import asyncio
import random
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from playwright.async_api import BrowserContext, Page, Route

from app.config import Settings
from app.database.models import Category, NewsItem
from app.dedupe.normalizer import canonicalize_url, extract_host, strip_html_entities
from app.logging_config import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# JavaScript chạy trong trang
# --------------------------------------------------------------------------

#: Lấy toàn bộ link bài viết trên trang danh sách.
EXTRACT_LINKS_JS = """
(selector) => {
  const out = [];
  const seen = new Set();
  for (const anchor of document.querySelectorAll(selector || 'a[href]')) {
    const href = anchor.href;
    if (!href || !href.startsWith('http')) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    out.push({ href, text: (anchor.textContent || '').replace(/\\s+/g, ' ').trim() });
  }
  return out;
}
"""

#: Trích xuất metadata bài viết: JSON-LD -> meta tags -> DOM.
EXTRACT_ARTICLE_JS = """
() => {
  const result = {
    title: null, author: null, published_at: null, description: null,
    image_url: null, article_body: null, site_name: null, canonical: null,
  };

  const clean = (value) => {
    if (value === null || value === undefined) return null;
    const text = String(value).replace(/\\s+/g, ' ').trim();
    return text.length ? text : null;
  };

  const metaContent = (selectors) => {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      if (node) {
        const value = node.getAttribute('content') || node.getAttribute('datetime') || node.textContent;
        const cleaned = clean(value);
        if (cleaned) return cleaned;
      }
    }
    return null;
  };

  const pickArticle = (node, depth) => {
    if (!node || typeof node !== 'object' || depth > 4) return null;
    if (Array.isArray(node)) {
      for (const child of node) {
        const found = pickArticle(child, depth + 1);
        if (found) return found;
      }
      return null;
    }
    const rawType = node['@type'];
    const types = Array.isArray(rawType) ? rawType : (rawType ? [rawType] : []);
    const isArticle = types.some((type) =>
      /^(NewsArticle|Article|BlogPosting|TechArticle|Report|WebPage|AnalysisNewsArticle)$/i.test(String(type))
    );
    if (isArticle) return node;
    for (const key of ['@graph', 'mainEntity', 'mainEntityOfPage', 'itemListElement', 'hasPart']) {
      if (key in node) {
        const found = pickArticle(node[key], depth + 1);
        if (found) return found;
      }
    }
    return null;
  };

  const authorName = (value, depth) => {
    if (!value || depth > 3) return null;
    if (typeof value === 'string') return clean(value);
    if (Array.isArray(value)) {
      for (const entry of value) {
        const name = authorName(entry, depth + 1);
        if (name) return name;
      }
      return null;
    }
    if (typeof value === 'object') {
      return clean(value.name || value['@id'] || (Array.isArray(value.url) ? value.url[0] : value.url));
    }
    return null;
  };

  const imageUrl = (value, depth) => {
    if (!value || depth > 3) return null;
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) {
      for (const entry of value) {
        const found = imageUrl(entry, depth + 1);
        if (found) return found;
      }
      return null;
    }
    if (typeof value === 'object') return clean(value.url || value.contentUrl);
    return null;
  };

  // --- 1. JSON-LD ---
  let ld = null;
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      ld = pickArticle(JSON.parse(script.textContent || ''), 0);
    } catch (error) {
      ld = null;
    }
    if (ld) break;
  }
  if (ld) {
    result.title = clean(ld.headline || ld.name || ld.alternativeHeadline);
    result.author = authorName(ld.author || ld.creator, 0);
    result.published_at = clean(
      ld.datePublished || ld.dateCreated || ld.dateModified ||
      (ld.datePublished instanceof Object ? null : null)
    );
    result.description = clean(ld.description || ld.abstract);
    result.image_url = imageUrl(ld.image || ld.thumbnailUrl, 0);
    result.article_body = clean(ld.articleBody);
    result.site_name = clean(ld.publisher && (ld.publisher.name || ld.publisher));
    const node = document.querySelector('link[rel="canonical"]');
    result.canonical = node ? clean(node.getAttribute('href')) : null;
  }

  // --- 2. Meta tags ---
  result.title = result.title || metaContent([
    'meta[property="og:title"]', 'meta[name="twitter:title"]',
    'meta[name="title"]',
  ]) || clean((document.querySelector('h1') || {}).textContent);

  result.description = result.description || metaContent([
    'meta[property="og:description"]', 'meta[name="twitter:description"]',
    'meta[name="description"]',
  ]);

  result.published_at = result.published_at || metaContent([
    'meta[property="article:published_time"]', 'meta[name="article:published_time"]',
    'meta[name="pubdate"]', 'meta[name="publish-date"]', 'meta[name="date"]',
    'time[datetime]', '[itemprop="datePublished"]',
  ]);

  result.author = result.author || metaContent([
    'meta[name="author"]', 'meta[property="article:author"]', 'meta[name="byl"]',
    '[rel="author"]', '[itemprop="author"] [itemprop="name"]', '.byline [rel="author"]',
  ]);

  result.image_url = result.image_url || metaContent([
    'meta[property="og:image"]', 'meta[name="twitter:image"]', 'meta[name="twitter:image:src"]',
  ]);

  result.site_name = result.site_name || metaContent([
    'meta[property="og:site_name"]', 'meta[name="application-name"]',
  ]);

  if (!result.canonical) {
    const node = document.querySelector('link[rel="canonical"]');
    result.canonical = node ? clean(node.getAttribute('href')) : null;
  }

  // --- 3. Thân bài ---
  if (!result.article_body) {
    const bodyNode = document.querySelector(
      '[itemprop="articleBody"], .article-content, .article__body, .post-content,' +
      ' .entry-content, .story-content, #article-body, main article, article'
    );
    if (bodyNode) {
      const paragraphs = Array.from(bodyNode.querySelectorAll('p'))
        .map((p) => (p.textContent || '').replace(/\\s+/g, ' ').trim())
        .filter((text) => text.length > 40);
      result.article_body = clean(paragraphs.join(' ').slice(0, 6000));
    }
  }

  return result;
}
"""

#: Lấy thông tin fingerprint của browser (dùng cho sub-command `probe`).
FINGERPRINT_JS = """
() => {
  const canvas = document.createElement('canvas');
  const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
  let renderer = null;
  let vendor = null;
  try {
    if (gl) {
      const info = gl.getExtension('WEBGL_debug_renderer_info');
      renderer = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
      vendor = info ? gl.getParameter(info.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR);
    }
  } catch (error) { /* ignore */ }
  return {
    user_agent: navigator.userAgent,
    platform: navigator.platform,
    vendor: navigator.vendor,
    language: navigator.language,
    languages: Array.from(navigator.languages || []),
    hardware_concurrency: navigator.hardwareConcurrency,
    device_memory: navigator.deviceMemory || null,
    max_touch_points: navigator.maxTouchPoints,
    webdriver: navigator.webdriver,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: { width: window.screen.width, height: window.screen.height, color_depth: window.screen.colorDepth, avail_width: window.screen.availWidth },
    viewport: { inner_width: window.innerWidth, inner_height: window.innerHeight, device_pixel_ratio: window.devicePixelRatio },
    webgl_renderer: renderer,
    webgl_vendor: vendor,
    cookies_enabled: navigator.cookieEnabled,
    do_not_track: navigator.doNotTrack,
  };
}
"""

BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


class RobotsGuard:
    """Tôn trọng robots.txt, tự tải và cache theo host."""

    def __init__(self, user_agent: str, enabled: bool = True) -> None:
        self._user_agent = user_agent
        self._enabled = enabled
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, url: str) -> bool:
        """True nếu robots.txt cho phép tải URL này."""
        if not self._enabled:
            return True
        parts = urlsplit(url)
        if not parts.netloc:
            return True
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._lock:
            if origin not in self._parsers:
                self._parsers[origin] = await self._load(origin)
        parser = self._parsers.get(origin)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self._user_agent, url)
        except Exception as exc:  # noqa: BLE001 - robots lỗi thì cho qua nhưng log
            logger.warning("Không đọc được robots.txt cho %s: %s", origin, exc)
            return True

    async def _load(self, origin: str) -> RobotFileParser | None:
        parser = RobotFileParser()
        robots_url = f"{origin}/robots.txt"
        try:
            response = await asyncio.to_thread(_http_get, robots_url)
        except Exception as exc:  # noqa: BLE001 - không có robots.txt là bình thường
            logger.debug("Không tải được robots.txt %s: %s", robots_url, exc)
            return None
        if response is None or not response[0]:
            logger.debug("robots.txt rỗng hoặc lỗi HTTP: %s", robots_url)
            return None
        parser.parse(response[1].splitlines())
        logger.debug("Đã nạp robots.txt: %s (%d dòng)", robots_url, len(response[1].splitlines()))
        return parser


def _http_get(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """GET đồng bộ tối giản (chạy trong thread) để tải robots.txt."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AI-Crypto-News-Crawler/1.0 (+https://example.local/bot)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status < 400, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code < 400, ""
    except Exception:  # noqa: BLE001
        return False, ""


# --------------------------------------------------------------------------
# Định nghĩa source
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Khai báo cấu hình riêng cho từng website.

    Đây chính là "adapter" của mỗi nguồn: chọn link bài viết, lọc đường dẫn
    không được phép theo robots.txt, và tốc độ ghé trang.
    """

    name: str
    category: Category
    start_url: str
    host: str
    link_selector: str = "a[href]"
    #: Regex khớp URL bài viết trên host này.
    article_pattern: str = r"/\d{4}/|/20\d{2}/|/[a-z0-9-]{20,}"
    #: Regex của các đường dẫn bị loại (theo robots.txt của từng site).
    deny_patterns: tuple[str, ...] = ()
    request_delay_seconds: float | None = None
    max_articles: int | None = None
    #: Timeout riêng cho ``page.goto`` của nguồn này (một số site rất chậm).
    page_timeout_ms: int | None = None
    #: Tổng thời gian dành cho cả nguồn. Mặc định lấy từ ``SOURCE_TIMEOUT_SECONDS``;
    #: site chậm nên khai báo riêng, ví dụ CoinDesk cần nhiều hơn cho 12 bài.
    source_timeout_seconds: float | None = None
    wait_selector: str | None = None
    scroll_rounds: int | None = None
    extra_wait_ms: int = 0
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def compiled_pattern(self) -> re.Pattern[str]:
        return _compile_pattern(self.article_pattern)

    @property
    def compiled_denies(self) -> tuple[re.Pattern[str], ...]:
        return tuple(_compile_pattern(pattern) for pattern in self.deny_patterns)

    def is_article_url(self, url: str) -> bool:
        """URL có phải bài viết hợp lệ của nguồn này không."""
        if extract_host(url) != self.host:
            return False
        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        for deny in self.compiled_denies:
            if deny.search(target):
                return False
        return bool(self.compiled_pattern.search(parts.path))

    @property
    def crawl_delay(self) -> float:
        if self.request_delay_seconds is not None:
            return self.request_delay_seconds
        return 0.0

    def navigation_timeout_ms(self, default_ms: int) -> int:
        """Timeout cho ``page.goto`` (dùng giá trị riêng của nguồn nếu có)."""
        return self.page_timeout_ms or default_ms

    def source_budget_seconds(self, default_seconds: float) -> float:
        """Tổng ngân sách thời gian cho nguồn này (dùng giá trị riêng nếu có)."""
        return self.source_timeout_seconds or default_seconds


_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    cached = _PATTERN_CACHE.get(pattern)
    if cached is None:
        cached = re.compile(pattern, re.IGNORECASE)
        _PATTERN_CACHE[pattern] = cached
    return cached


class NewsSource(ABC):
    """Interface chung cho mọi nguồn tin."""

    def __init__(self, spec: SourceSpec, settings: Settings) -> None:
        self.spec = spec
        self.settings = settings

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def category(self) -> Category:
        return self.spec.category

    @property
    def start_url(self) -> str:
        return self.spec.start_url

    @abstractmethod
    async def crawl(self, context: BrowserContext) -> list[NewsItem]:
        """Lấy danh sách bài mới từ trang chủ/trang chuyên mục."""

    def __repr__(self) -> str:  # pragma: no cover - chỉ phục vụ log
        return f"<{type(self).__name__} {self.name} ({self.category.value})>"


class ListPageSource(NewsSource):
    """Adapter mặc định: vào trang danh sách, mở từng bài để lấy metadata."""

    def __init__(self, spec: SourceSpec, settings: Settings, robots: RobotsGuard) -> None:
        super().__init__(spec, settings)
        self._robots = robots
        #: URL gốc của trang listing, dùng để nối link tương đối thành tuyệt đối.
        self._base_url = spec.start_url

    # -- các bước có thể ghi đè trong adapter riêng ----------------------

    async def on_listing_loaded(self, page: Page) -> None:
        """Hook gọi sau khi trang danh sách tải xong (chờ selector, cuộn trang...)."""
        if self.spec.wait_selector:
            try:
                await page.wait_for_selector(self.spec.wait_selector, timeout=self.settings.browser_timeout_ms)
            except Exception as exc:  # noqa: BLE001 - không có selector vẫn crawl tiếp
                logger.debug("[%s] Không thấy selector %s: %s", self.name, self.spec.wait_selector, exc)
        rounds = self.settings.scroll_rounds if self.spec.scroll_rounds is None else self.spec.scroll_rounds
        for _ in range(rounds):
            await page.mouse.wheel(0, 2200)
            await page.wait_for_timeout(400)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception as exc:  # noqa: BLE001 - trang có analytics/socket thường không bao giờ idle
            logger.debug("[%s] Trang listing chưa network-idle sau 5s: %s", self.name, exc)

    async def on_article_loaded(self, page: Page) -> None:
        """Hook gọi sau khi trang bài viết tải xong."""
        if self.spec.extra_wait_ms:
            await page.wait_for_timeout(self.spec.extra_wait_ms)

    def build_item(self, payload: dict[str, object], url: str) -> NewsItem | None:
        """Chuyển dữ liệu trích xuất được thành :class:`NewsItem` (None nếu thiếu tiêu đề)."""
        title = strip_html_entities(str(payload.get("title") or "")).strip()
        if len(title) < 8:
            return None
        title = _PUNCT_TAIL_RE.sub("", title).strip()
        author = strip_html_entities(str(payload.get("author") or "")).strip() or None
        if author and len(author) > 120:
            author = author[:120]
        body = payload.get("article_body")
        body_text = strip_html_entities(str(body)).strip() if body else ""
        summary = strip_html_entities(str(payload.get("description") or "")).strip() or None
        if summary:
            summary = summary[:1200]
        image_url = str(payload.get("image_url") or "").strip() or None
        if image_url and not image_url.startswith(("http://", "https://")):
            image_url = None
        return NewsItem(
            title=title,
            url=url,
            source=self.name,
            category=self.category,
            author=author,
            published_at=payload.get("published_at"),  # type: ignore[arg-type]
            summary=summary,
            image_url=image_url,
            content=body_text[:8000] or None,
        )

    # -- luồng crawl -----------------------------------------------------

    async def crawl(self, context: BrowserContext) -> list[NewsItem]:
        page = await context.new_page()
        items: list[NewsItem] = []
        try:
            await page.goto(
                self.start_url,
                wait_until="domcontentloaded",
                timeout=self.spec.navigation_timeout_ms(self.settings.browser_timeout_ms),
            )
            await self.on_listing_loaded(page)
            # Sau redirect, URL thật mới là gốc để nối link tương đối.
            self._base_url = page.url or self.start_url

            links: list[dict[str, str]] = await page.evaluate(EXTRACT_LINKS_JS, self.spec.link_selector)
            candidates = self._filter_links(links)
            limit = self.settings.max_articles_per_source if self.spec.max_articles is None else self.spec.max_articles
            candidates = candidates[:limit]
            logger.info(
                "[%s] Tìm thấy %d link, %d link hợp lệ sau khi lọc, sẽ mở %d bài",
                self.name, len(links), len(candidates), len(candidates),
            )
            if not candidates:
                logger.warning(
                    "[%s] Không lấy được link bài nào từ %s — có thể site đổi layout, "
                    "link tương đối, hoặc đang chặn truy cập tự động",
                    self.name, self.start_url,
                )

            for url in candidates:
                item = await self._crawl_single(context, url)
                if item is not None:
                    items.append(item)
        finally:
            await page.close()
        return items

    def _filter_links(self, links: list[dict[str, str]]) -> list[str]:
        """Lọc link theo host, pattern bài viết, deny list và robots.txt (đã cache).

        Link tương đối (``/tech/2026/09/29/slug``) được nối với URL trang
        listing trước khi kiểm tra host, vì nhiều site dùng link tương đối.
        """
        seen: set[str] = set()
        result: list[str] = []
        for link in links:
            href = str(link.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            url = urljoin(self._base_url, href)
            if not self.spec.is_article_url(url):
                continue
            canonical = canonicalize_url(url)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            result.append(url)
        return result

    async def _crawl_single(self, context: BrowserContext, url: str) -> NewsItem | None:
        if not await self._robots.is_allowed(url):
            logger.info("[%s] Bỏ qua do robots.txt: %s", self.name, url)
            return None
        page = await context.new_page()
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.spec.navigation_timeout_ms(self.settings.browser_timeout_ms),
            )
            await self.on_article_loaded(page)
            payload = await page.evaluate(EXTRACT_ARTICLE_JS)
            if not isinstance(payload, dict):
                return None
            return self.build_item(payload, page.url)
        except Exception as exc:  # noqa: BLE001 - 1 bài lỗi không được làm hỏng cả nguồn
            logger.warning("[%s] Không crawl được %s: %s", self.name, url, exc)
            return None
        finally:
            await page.close()
            await self._respect_delay()

    async def _respect_delay(self) -> None:
        delay = self.spec.crawl_delay or self.settings.request_delay_seconds
        if delay > 0:
            await asyncio.sleep(delay * random.uniform(0.7, 1.3))  # noqa: S311 - jitter chống dồn request


_PUNCT_TAIL_RE = re.compile(r"[\s\|\-–—:»>]+$")


# --------------------------------------------------------------------------
# Tiện ích dùng chung
# --------------------------------------------------------------------------


async def block_heavy_resources(context: BrowserContext, enabled: bool = True) -> None:
    """Chặn ảnh/font/media để crawl nhanh và nhẹ hơn (bật/tắt qua config)."""
    if not enabled:
        return

    async def _handler(route: Route) -> None:
        try:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception as exc:  # noqa: BLE001 - route đã đóng thì bỏ qua
            logger.debug("Route handler lỗi: %s", exc)

    await context.route("**/*", _handler)


async def with_retry(
    operation: Callable[[], Awaitable[object]],
    attempts: int,
    backoff_seconds: float,
    label: str,
) -> tuple[object | None, Exception | None]:
    """Chạy một thao tác có retry + backoff. Trả về ``(kết quả, lỗi cuối cùng)``."""
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await operation(), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - đây là điểm bắt lỗi có chủ đích
            last_error = exc
            if attempt >= attempts:
                break
            delay = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "%s lỗi (lần %d/%d), thử lại sau %.1fs: %s",
                label, attempt, attempts, delay, exc,
            )
            await asyncio.sleep(delay)
    return None, last_error
