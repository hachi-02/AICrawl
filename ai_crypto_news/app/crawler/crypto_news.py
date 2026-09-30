"""Các adapter nguồn tin chủ đề Crypto.

Deny pattern của từng nguồn bám đúng robots.txt đã kiểm tra thực tế
(ví dụ Bitcoin Magazine yêu cầu Crawl-delay 5s nên có delay riêng).
"""

from __future__ import annotations

from app.config import Settings
from app.crawler.base import ListPageSource, NewsSource, RobotsGuard, SourceSpec
from app.database.models import Category

# --------------------------------------------------------------------------
# Khai báo nguồn
# --------------------------------------------------------------------------

COINDESK = SourceSpec(
    name="coindesk",
    category=Category.CRYPTO,
    start_url="https://www.coindesk.com/",
    host="coindesk.com",
    article_pattern=r"^/(?!amp$)[a-z0-9-]+/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/?$",
    # robots.txt: Disallow /api/ /search /?s= /?q= /*/amp /layer2/ /podcasts
    deny_patterns=(
        r"^/(api|search|auth|login|layer2|consensus-magazine|podcasts|events|arc|exchange-api|sp|about)/",
        r"[?&](s|q|t)=",
        r"/amp/?$",
    ),
    wait_selector="a[href*='/20']",
    request_delay_seconds=1.5,
    # Đo thực tế: ~9s/bài, có bài treo tới 25s. Giới hạn 8 bài + budget riêng
    # để không bao giờ mất trắng cả nguồn vì ngân sách 90s mặc định.
    max_articles=8,
    page_timeout_ms=60_000,
    source_timeout_seconds=180.0,
    description="CoinDesk - tiền tệ số",
)

COINTELEGRAPH = SourceSpec(
    name="cointelegraph",
    category=Category.CRYPTO,
    start_url="https://cointelegraph.com/",
    host="cointelegraph.com",
    article_pattern=r"^/(news|press-releases|top-news|events)/[a-z0-9][a-z0-9-]{18,}/?$",
    # robots.txt: Disallow /api/ /wp-admin/ /profile /*?ref= *_token= *_ga *_fbclid
    deny_patterns=(
        r"^/(api|wp-|wp-admin|profile|login|tag|press-releases$)",
        r"[?&](ref|token|_ga|_twitter|fbclid|portalId|dates)=",
    ),
    wait_selector="a[href*='/news/']",
    description="Cointelegraph",
)

THE_BLOCK = SourceSpec(
    name="theblock",
    category=Category.CRYPTO,
    start_url="https://www.theblock.co/",
    host="theblock.co",
    article_pattern=r"^/post/\d+/[a-z0-9-]{15,}/?$",
    # robots.txt: Disallow /search /api/ /preview/ /wp-json/ /ping /ws/
    deny_patterns=(r"^/(search|api|preview|wp-json|ping|ws|tag|news-flash|user)/",),
    wait_selector="a[href*='/post/']",
    description="The Block",
)

DECRYPT = SourceSpec(
    name="decrypt",
    category=Category.CRYPTO,
    start_url="https://decrypt.co/news",
    host="decrypt.co",
    article_pattern=r"^(/news)?/\d{5,}/[a-z0-9-]{12,}/?$",
    # robots.txt: chỉ có Sitemap, mọi đường dẫn bài đều được phép
    deny_patterns=(r"^/(news-sitemap|sitemap|tag|author|video|videos|events)/",),
    wait_selector="a[href]",
    description="Decrypt",
)

BITCOIN_MAGAZINE = SourceSpec(
    name="bitcoinmagazine",
    category=Category.CRYPTO,
    start_url="https://bitcoinmagazine.com/",
    host="bitcoinmagazine.com",
    article_pattern=r"^/[a-z0-9-]+/[a-z0-9-]{20,}/?$",
    # robots.txt: Crawl-delay 5, Disallow /private-keys /json/ /wp-admin/ /sitemap/20*
    deny_patterns=(
        r"^/(private-keys|public-keys|json|wp-admin|wp-content|json-api|sitemap|search|tag|author|events)/",
    ),
    # robots.txt yêu cầu Crawl-delay: 5
    request_delay_seconds=5.0,
    max_articles=8,
    description="Bitcoin Magazine",
)

CRYPTOSLATE = SourceSpec(
    name="cryptoslate",
    category=Category.CRYPTO,
    start_url="https://cryptoslate.com/",
    host="cryptoslate.com",
    article_pattern=r"^/(news|insights|learn|press-releases)/[a-z0-9-]{18,}/?$",
    # robots.txt: Disallow /*?s= /*?utm_* /*?tribe_*
    deny_patterns=(r"^/(wp-admin|wp-content|tag|author|category|events)/", r"[?&](s|utm_[a-z]+|tribe_[a-z]+)="),
    wait_selector="a[href]",
    description="CryptoSlate",
)

BEINCRYPTO = SourceSpec(
    name="beincrypto",
    category=Category.CRYPTO,
    start_url="https://beincrypto.com/news/",
    host="beincrypto.com",
    article_pattern=r"^/(news/)?[a-z0-9][a-z0-9-]{20,}/?$",
    # robots.txt: Disallow /wp-admin/ /wp-json/ /search/ */tickers /graphql /ceranking
    deny_patterns=(
        r"^/(wp-admin|wp-json|search|graphql|ceranking|price|_next|about|authors|category|explained|learn|events|tools)/",
        r"/tickers?/?$",
        r"[?&]amount=",
    ),
    wait_selector="a[href]",
    max_articles=10,
    description="BeInCrypto",
)

CRYPTO_SPECS: tuple[SourceSpec, ...] = (
    COINDESK,
    COINTELEGRAPH,
    THE_BLOCK,
    DECRYPT,
    BITCOIN_MAGAZINE,
    CRYPTOSLATE,
    BEINCRYPTO,
)


# --------------------------------------------------------------------------
# Adapter cho từng nguồn
# --------------------------------------------------------------------------


class CoinDesk(ListPageSource):
    """CoinDesk: URL bài dạng /section/YYYY/MM/DD/slug/."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(600)
        await super().on_listing_loaded(page)


class Cointelegraph(ListPageSource):
    """Cointelegraph: chỉ lấy mục /news/ và /press-releases/."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(600)
        await super().on_listing_loaded(page)


class TheBlock(ListPageSource):
    """The Block: URL bài dạng /post/{id}/{slug}."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(800)
        await super().on_listing_loaded(page)


class Decrypt(ListPageSource):
    """Decrypt: URL bài dạng /{id}/{slug}."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(600)
        await super().on_listing_loaded(page)


class BitcoinMagazine(ListPageSource):
    """Bitcoin Magazine: cần chờ giữa các request vì robots.txt yêu cầu Crawl-delay 5."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(500)
        await super().on_listing_loaded(page)


class CryptoSlate(ListPageSource):
    """CryptoSlate: lấy mục /news/ và /insights/."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(600)
        await super().on_listing_loaded(page)


class BeInCrypto(ListPageSource):
    """BeInCrypto: WordPress + Next.js, bài nằm trong /news/."""

    async def on_listing_loaded(self, page):  # type: ignore[no-untyped-def]
        await page.wait_for_timeout(800)
        await super().on_listing_loaded(page)


_SOURCE_CLASSES: dict[str, type[ListPageSource]] = {
    "coindesk": CoinDesk,
    "cointelegraph": Cointelegraph,
    "theblock": TheBlock,
    "decrypt": Decrypt,
    "bitcoinmagazine": BitcoinMagazine,
    "cryptoslate": CryptoSlate,
    "beincrypto": BeInCrypto,
}


def create_crypto_sources(settings: Settings, robots: RobotsGuard) -> list[NewsSource]:
    """Tạo danh sách adapter Crypto."""
    sources: list[NewsSource] = []
    for spec in CRYPTO_SPECS:
        source_class = _SOURCE_CLASSES.get(spec.name, ListPageSource)
        sources.append(source_class(spec, settings, robots))
    return sources


__all__ = ["CRYPTO_SPECS", "create_crypto_sources", "Category"]
