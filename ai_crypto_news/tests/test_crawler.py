"""Test phần crawler: chọn nguồn, lọc link, timeout riêng từng nguồn."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.crawler.base import ListPageSource, SourceSpec
from app.crawler.browser import CHROMIUM_ARGS
from app.crawler.sources import create_sources
from app.database.models import Category
from tests.conftest import settings as settings_fixture  # noqa: F401 - dùng fixture


def _source(spec: SourceSpec, settings: Settings) -> ListPageSource:
    from app.crawler.base import RobotsGuard

    return ListPageSource(spec, settings, RobotsGuard(user_agent="*", enabled=False))


class TestCreateSources:
    def test_all_sources_enabled_by_default(self, settings: Settings) -> None:
        settings = settings.model_copy(update={"disabled_sources": "", "enabled_sources": ""})
        sources, _ = create_sources(settings)
        names = {source.name for source in sources}
        assert {"techcrunch", "theverge", "coindesk", "cointelegraph"} <= names

    def test_disabled_sources_are_skipped(self, settings: Settings) -> None:
        settings = settings.model_copy(update={"disabled_sources": "coindesk, theblock"})
        sources, _ = create_sources(settings)
        names = {source.name for source in sources}
        assert "coindesk" not in names
        assert "theblock" not in names
        assert "techcrunch" in names

    def test_only_filter_wins(self, settings: Settings) -> None:
        settings = settings.model_copy(update={"disabled_sources": ""})
        sources, _ = create_sources(settings, only=["coindesk"])
        assert [source.name for source in sources] == ["coindesk"]

    def test_category_filter(self, settings: Settings) -> None:
        settings = settings.model_copy(update={"disabled_sources": ""})
        sources, _ = create_sources(settings, category=Category.AI)
        assert sources and all(source.category is Category.AI for source in sources)


class TestSourceSpec:
    def test_chromium_does_not_hide_automation_or_disable_sandbox(self) -> None:
        args = " ".join(CHROMIUM_ARGS).lower()
        assert "automationcontrolled" not in args
        assert "no-sandbox" not in args

    def test_navigation_timeout_defaults_to_browser_timeout(self, settings: Settings) -> None:
        spec = SourceSpec(name="x", category=Category.AI, start_url="https://x.com/", host="x.com")
        assert spec.navigation_timeout_ms(settings.browser_timeout_ms) == settings.browser_timeout_ms

    def test_navigation_timeout_uses_source_override(self, settings: Settings) -> None:
        spec = SourceSpec(
            name="x", category=Category.AI, start_url="https://x.com/", host="x.com", page_timeout_ms=60_000
        )
        assert spec.navigation_timeout_ms(settings.browser_timeout_ms) == 60_000

    def test_slow_sources_declare_longer_timeout(self, settings: Settings) -> None:
        settings = settings.model_copy(update={"disabled_sources": ""})
        sources, _ = create_sources(settings)
        by_name = {source.name: source.spec for source in sources}
        assert by_name["coindesk"].page_timeout_ms == 60_000
        assert by_name["wired"].page_timeout_ms == 60_000

    def test_slow_sources_get_own_source_budget(self, settings: Settings) -> None:
        """Site chậm cần ngân sách nguồn riêng, không dùng mặc định 90s."""
        spec = SourceSpec(name="x", category=Category.AI, start_url="https://x.com/", host="x.com")
        assert spec.source_budget_seconds(90.0) == 90.0
        spec = SourceSpec(
            name="x", category=Category.AI, start_url="https://x.com/", host="x.com", source_timeout_seconds=180.0
        )
        assert spec.source_budget_seconds(90.0) == 180.0
        settings = settings.model_copy(update={"disabled_sources": ""})
        sources, _ = create_sources(settings)
        by_name = {source.name: source.spec for source in sources}
        assert by_name["coindesk"].source_budget_seconds(settings.source_timeout_seconds) > 90
        assert by_name["wired"].source_budget_seconds(settings.source_timeout_seconds) > 90
        # Giới hạn số bài để thời gian nằm trong budget
        assert by_name["coindesk"].max_articles == 8
        assert by_name["wired"].max_articles == 8

    async def test_adapter_awaits_base_listing_hook(self, settings: Settings) -> None:
        """Hook con phải await hook cha, không được trả về coroutine chưa chạy."""
        settings = settings.model_copy(update={"disabled_sources": "", "scroll_rounds": 1})
        sources, _ = create_sources(settings, only=["theverge"])
        source = sources[0]
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        page.wait_for_selector = AsyncMock()
        page.wait_for_load_state = AsyncMock(side_effect=TimeoutError("chưa idle"))
        page.mouse.wheel = AsyncMock()

        await source.on_listing_loaded(page)

        page.wait_for_selector.assert_awaited_once()
        page.mouse.wheel.assert_awaited_once()
        page.wait_for_load_state.assert_awaited_once()

    def test_theverge_has_budget_for_slow_listing(self, settings: Settings) -> None:
        settings = settings.model_copy(update={"disabled_sources": ""})
        sources, _ = create_sources(settings, only=["theverge"])
        spec = sources[0].spec
        assert spec.page_timeout_ms == 60_000
        assert spec.source_timeout_seconds == 180.0
        assert spec.max_articles == 8


class TestFilterLinks:
    spec = SourceSpec(
        name="coindesk",
        category=Category.CRYPTO,
        start_url="https://www.coindesk.com/",
        host="coindesk.com",
        article_pattern=r"^/(?!amp$)[a-z0-9-]+/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/?$",
    )

    def test_relative_links_are_resolved(self, settings: Settings) -> None:
        """CoinDesk dùng link tương đối; nếu không urljoin thì mất hết bài."""
        source = _source(self.spec, settings)
        result = source._filter_links(
            [{"href": "/tech/2026/09/29/zcash-faster-private-payment-code-is-rebuilt"}]
        )
        assert result == [
            "https://www.coindesk.com/tech/2026/09/29/zcash-faster-private-payment-code-is-rebuilt"
        ]

    def test_absolute_links_still_work(self, settings: Settings) -> None:
        source = _source(self.spec, settings)
        result = source._filter_links(
            [{"href": "https://www.coindesk.com/markets/2026/09/29/aave-leads-defi-higher"}]
        )
        assert len(result) == 1

    def test_denied_and_amp_links_are_dropped(self, settings: Settings) -> None:
        """Dùng chính deny pattern thật của CoinDesk (theo robots.txt)."""
        from app.crawler.crypto_news import COINDESK

        source = _source(COINDESK, settings)
        result = source._filter_links(
            [
                {"href": "/api/2026/09/29/some-long-slug"},
                {"href": "/search/2026/09/29/some-long-slug"},
                {"href": "/tech/2026/09/29/some-long-slug?q=bitcoin"},
                {"href": "/tech/2026/09/29/some-long-slug/amp"},
            ]
        )
        assert result == []

    def test_real_coindesk_allowlist(self, settings: Settings) -> None:
        """Link bài hợp lệ lấy từ trang chủ CoinDesk phải đi qua được bộ lọc."""
        from app.crawler.crypto_news import COINDESK

        source = _source(COINDESK, settings)
        result = source._filter_links(
            [{"href": "/tech/2026/09/29/zcash-s-faster-private-payment-code-is-being-rebuilt"}]
        )
        assert len(result) == 1

    def test_non_article_and_javascript_links_are_dropped(self, settings: Settings) -> None:
        source = _source(self.spec, settings)
        result = source._filter_links(
            [
                {"href": "/markets"},
                {"href": "#top"},
                {"href": "javascript:void(0)"},
                {"href": "mailto:a@b.com"},
                {"href": ""},
            ]
        )
        assert result == []

    def test_duplicate_links_are_collapsed(self, settings: Settings) -> None:
        source = _source(self.spec, settings)
        result = source._filter_links(
            [
                {"href": "/tech/2026/09/29/same-story-slug"},
                {"href": "https://www.coindesk.com/tech/2026/09/29/same-story-slug"},
                {"href": "https://coindesk.com/tech/2026/09/29/same-story-slug/"},
            ]
        )
        assert len(result) == 1

    def test_other_host_is_rejected(self, settings: Settings) -> None:
        source = _source(self.spec, settings)
        result = source._filter_links([{"href": "https://evil.example/tech/2026/09/29/some-long-slug"}])
        assert result == []

    def test_base_url_follows_redirect(self, settings: Settings) -> None:
        """Sau redirect sang domain khác, link tương đối phải nối theo domain mới."""
        source = _source(self.spec, settings)
        source._base_url = "https://www.coindesk.com/latest-crypto-news/"
        result = source._filter_links([{"href": "/tech/2026/09/29/redirected-story-slug"}])
        assert result == ["https://www.coindesk.com/tech/2026/09/29/redirected-story-slug"]
