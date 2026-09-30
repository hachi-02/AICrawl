"""Các adapter nguồn tin chủ đề AI.

Mỗi nguồn là một :class:`SourceSpec` riêng biểu diễn đúng cấu trúc URL và
quy tắc robots.txt của website đó, nên thêm nguồn mới chỉ cần thêm 1 khối.
"""

from __future__ import annotations

from app.config import Settings
from app.crawler.base import ListPageSource, NewsSource, RobotsGuard, SourceSpec
from app.database.models import Category

# --------------------------------------------------------------------------
# Khai báo nguồn
# --------------------------------------------------------------------------

TECHCRUNCH_AI = SourceSpec(
    name="techcrunch",
    category=Category.AI,
    start_url="https://techcrunch.com/category/artificial-intelligence/",
    host="techcrunch.com",
    article_pattern=r"^/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/?$",
    # robots.txt: Disallow /search/, /?s=, /wp-admin/
    deny_patterns=(r"^/(search|tag|author|events|wp-|category)/", r"[?&]s="),
    wait_selector="a[href*='/20']",
    description="TechCrunch - chuyên mục Artificial Intelligence",
)

VENTUREBEAT_AI = SourceSpec(
    name="venturebeat",
    category=Category.AI,
    start_url="https://venturebeat.com/category/ai/",
    host="venturebeat.com",
    article_pattern=r"^/[a-z0-9-]+/[a-z0-9-]{18,}/?$",
    # robots.txt: Disallow /search /_next/ /api/ /login /sponsored-posts
    deny_patterns=(r"^/(search|api|login|logout|sponsored-posts|author|events|_next|wp-)",),
    wait_selector="a[href]",
    description="VentureBeat - chuyên mục AI",
)

THE_VERGE_AI = SourceSpec(
    name="theverge",
    category=Category.AI,
    start_url="https://www.theverge.com/ai-artificial-intelligence",
    host="theverge.com",
    article_pattern=r"(/[a-z0-9-]+)*/(?:\d{4}/\d{1,2}/\d{1,2}/\d+|\d+/[a-z0-9][a-z0-9-]{15,})/?$",
    # robots.txt: Disallow /search /account /login /share /users /newfanshot
    deny_patterns=(
        r"^/(search|account|login|logout|share|users|newfanshot|admin|chorus_auth|sso|auth)",
        r"\.(xml|json|js|css)$",
    ),
    wait_selector="a[href]",
    # Listing đôi lúc cần 30-60s và một số bài con có thể treo; giới hạn 8 bài
    # để retry không kéo dài cả vòng crawl.
    max_articles=8,
    page_timeout_ms=60_000,
    source_timeout_seconds=180.0,
    description="The Verge - chuyên mục AI",
)

MIT_TECH_REVIEW = SourceSpec(
    name="technologyreview",
    category=Category.AI,
    start_url="https://www.technologyreview.com/",
    host="technologyreview.com",
    article_pattern=r"^/\d{4}/\d{2}/\d{2}/\d{5,}/[a-z0-9-]+/?$",
    # robots.txt: Disallow /wp-admin/, /*.pdf$
    deny_patterns=(r"^/(wp-|topic/|tag/|author/|search/|events/)", r"\.pdf$"),
    wait_selector="a[href]",
    max_articles=10,
    description="MIT Technology Review",
)

WIRED_AI = SourceSpec(
    name="wired",
    category=Category.AI,
    start_url="https://www.wired.com/tag/artificial-intelligence/",
    host="wired.com",
    article_pattern=r"^/story/[a-z0-9][a-z0-9-]{18,}/?$",
    # robots.txt: Disallow /search /product/ /account/ /auth/ /cdn-cgi/ và mọi query string
    deny_patterns=(r"^/(search|product|account|user|auth|cdn-cgi|services|reject-all|review)/", r"\?"),
    wait_selector="a[href*='/story/']",
    # Đo thực tế: ~14s/bài, có bài treo tới 53s nên cần ngân sách riêng.
    max_articles=8,
    page_timeout_ms=60_000,
    source_timeout_seconds=210.0,
    description="WIRED - chuyên mục Artificial Intelligence",
)

AI_SPECS: tuple[SourceSpec, ...] = (
    TECHCRUNCH_AI,
    VENTUREBEAT_AI,
    THE_VERGE_AI,
    MIT_TECH_REVIEW,
    WIRED_AI,
)


# --------------------------------------------------------------------------
# Adapter cho từng nguồn
# --------------------------------------------------------------------------


class TechCrunchAI(ListPageSource):
    """TechCrunch: URL bài dạng /YYYY/MM/DD/slug/."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        # Trang chuyên mục chỉ render bài sau 1 nhịp JS nhỏ
        await page.wait_for_timeout(800)
        await super().on_listing_loaded(page)


class VentureBeatAI(ListPageSource):
    """VentureBeat: Next.js, cần chờ link bài xuất hiện."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(800)
        await super().on_listing_loaded(page)


class TheVergeAI(ListPageSource):
    """The Verge: danh sách bài nằm trong các khối /collections và /2026/..."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(600)
        await super().on_listing_loaded(page)


class MITTechReview(ListPageSource):
    """MIT Technology Review: trang chủ nhiều khu vực, chỉ giữ link dạng ngày/tháng/năm."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(500)
        await super().on_listing_loaded(page)


class WiredAI(ListPageSource):
    """WIRED: chỉ nhận link /story/... như robots.txt cho phép."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(500)
        await super().on_listing_loaded(page)


_SOURCE_CLASSES: dict[str, type[ListPageSource]] = {
    "techcrunch": TechCrunchAI,
    "venturebeat": VentureBeatAI,
    "theverge": TheVergeAI,
    "technologyreview": MITTechReview,
    "wired": WiredAI,
}


def create_ai_sources(settings: Settings, robots: RobotsGuard) -> list[NewsSource]:
    """Tạo danh sách adapter AI."""
    sources: list[NewsSource] = []
    for spec in AI_SPECS:
        source_class = _SOURCE_CLASSES.get(spec.name, ListPageSource)
        sources.append(source_class(spec, settings, robots))
    return sources


__all__ = ["AI_SPECS", "create_ai_sources", "Category"]
