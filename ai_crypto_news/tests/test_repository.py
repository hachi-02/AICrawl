"""Test lớp truy cập dữ liệu: insert, link nguồn, đánh dấu đã gửi, truy vấn, thống kê."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.database.models import Category
from app.database.repository import NewsRepository
from tests.conftest import NOW, make_item

CHAT = "@test_chat"


class TestInsert:
    async def test_app_setting_roundtrip(self, repository: NewsRepository) -> None:
        assert await repository.get_app_setting("auto_crawl_enabled") is None
        await repository.set_app_setting("auto_crawl_enabled", "0", now=NOW)
        assert await repository.get_app_setting("auto_crawl_enabled") == "0"
        await repository.set_app_setting("auto_crawl_enabled", "1", now=NOW)
        assert await repository.get_app_setting("auto_crawl_enabled") == "1"

    async def test_insert_returns_aligned_ids(self, repository: NewsRepository) -> None:
        items = [
            make_item("Bài số một về AI", url="https://techcrunch.com/1/"),
            make_item("Bài số hai về crypto", url="https://coindesk.com/2/", category=Category.CRYPTO),
        ]
        inserted = await repository.insert_items(items, now=NOW)
        assert [news_id for news_id, _ in inserted] == [1, 2]
        assert [item.title for _, item in inserted] == [items[0].title, items[1].title]

    async def test_insert_empty(self, repository: NewsRepository) -> None:
        assert await repository.insert_items([], now=NOW) == []

    async def test_duplicate_url_ignored(self, repository: NewsRepository) -> None:
        items = [make_item("Một tiêu đề", url="https://techcrunch.com/same/")]
        await repository.insert_items(items, now=NOW)
        assert await repository.insert_items(items, now=NOW) == []

    async def test_fingerprints_roundtrip(self, repository: NewsRepository) -> None:
        await repository.insert_items([make_item("OpenAI ships GPT-5 model to all users")], now=NOW)
        fingerprints = await repository.load_fingerprints(window_days=7, now=NOW)
        assert len(fingerprints) == 1
        fp = fingerprints[0]
        assert fp.source == "techcrunch"
        assert fp.category == Category.AI.value
        assert fp.simhash != 0
        assert "openai" in fp.title_tokens

    async def test_fingerprints_respect_window(self, repository: NewsRepository) -> None:
        old = make_item("Bài cũ xa", published_at=NOW - timedelta(days=40))
        await repository.insert_items([old], now=NOW - timedelta(days=40))
        assert await repository.load_fingerprints(window_days=7, now=NOW) == []


class TestLinkSource:
    async def test_link_creates_row_and_increments_count(self, repository: NewsRepository) -> None:
        (news_id, _), = await repository.insert_items(
            [make_item("Bitcoin ETF được SEC chấp thuận", source="coindesk")], now=NOW
        )
        added = await repository.link_source(
            news_id,
            make_item(
                "Bitcoin ETF được SEC chấp thuận",
                source="cointelegraph",
                url="https://cointelegraph.com/news/btc",
            ),
            now=NOW,
        )
        assert added
        record = await repository.get_record(news_id)
        assert record is not None
        assert record.source_count == 2
        assert len(record.extra_urls) == 1
        assert "cointelegraph" in record.extra_urls[0]
        # news_sources chỉ chứa nguồn phụ (nguồn chính nằm ở news.source)
        assert await repository.list_sources_for(news_id) == [
            "cointelegraph: https://cointelegraph.com/news/btc"
        ]

    async def test_link_same_url_twice_is_noop(self, repository: NewsRepository) -> None:
        (news_id, _), = await repository.insert_items([make_item("Một bài tin", source="coindesk")], now=NOW)
        item = make_item("Bài khác tiêu đề", source="decrypt", url="https://decrypt.co/x")
        assert await repository.link_source(news_id, item, now=NOW) is True
        assert await repository.link_source(news_id, item, now=NOW) is False

    async def test_link_unknown_id_returns_false(self, repository: NewsRepository) -> None:
        assert await repository.link_source(999, make_item("Không tồn tại"), now=NOW) is False

    async def test_link_primary_url_again_is_noop(self, repository: NewsRepository) -> None:
        """Cùng URL đã là URL chính thì không tạo dòng phụ (chống phình dữ liệu)."""
        (news_id, _), = await repository.insert_items(
            [make_item("Một bài tin", source="coindesk", url="https://coindesk.com/abc/2026/09/29/x/")],
            now=NOW,
        )
        again = make_item("Tiêu đề hơi khác", source="coindesk", url="https://coindesk.com/abc/2026/09/29/x/")
        assert await repository.link_source(news_id, again, now=NOW) is False
        record = await repository.get_record(news_id)
        assert record is not None
        assert record.source_count == 1
        assert record.extra_urls == []

    async def test_link_canonical_variant_of_primary_is_noop(self, repository: NewsRepository) -> None:
        """Biến thể trailing slash của URL chính vẫn phải bị bỏ qua."""
        (news_id, _), = await repository.insert_items(
            [make_item("Một bài tin", source="coindesk", url="https://coindesk.com/abc/2026/09/29/x")],
            now=NOW,
        )
        variant = make_item("Tiêu đề hơi khác", source="coindesk", url="https://coindesk.com/abc/2026/09/29/x/")
        assert await repository.link_source(news_id, variant, now=NOW) is False
        record = await repository.get_record(news_id)
        assert record is not None
        assert record.source_count == 1
        assert record.extra_urls == []

    async def test_link_same_source_different_url_keeps_count(self, repository: NewsRepository) -> None:
        """Cùng nguồn nhưng URL khác (bản in lại) không được tính thành nguồn thứ hai."""
        (news_id, _), = await repository.insert_items(
            [make_item("Bitcoin tăng mạnh", source="coindesk", url="https://coindesk.com/abc/2026/09/29/x/")],
            now=NOW,
        )
        reprint_ = make_item("Bitcoin tăng mạnh", source="coindesk", url="https://coindesk.com/abc/2026/09/29/y/")
        assert await repository.link_source(news_id, reprint_, now=NOW) is True
        record = await repository.get_record(news_id)
        assert record is not None
        assert record.source_count == 1
        assert len(record.extra_urls) == 1

    async def test_link_two_urls_same_secondary_source_counts_once(self, repository: NewsRepository) -> None:
        """source_count là số nguồn phân biệt, không phải số URL."""
        (news_id, _), = await repository.insert_items(
            [make_item("Bitcoin tăng mạnh", source="coindesk", url="https://coindesk.com/abc/2026/09/29/x/")],
            now=NOW,
        )
        first = make_item("Bitcoin tăng mạnh", source="decrypt", url="https://decrypt.co/news/one")
        second = make_item("Bitcoin tăng mạnh", source="decrypt", url="https://decrypt.co/news/two")
        assert await repository.link_source(news_id, first, now=NOW) is True
        assert await repository.link_source(news_id, second, now=NOW) is True
        record = await repository.get_record(news_id)
        assert record is not None
        assert record.source_count == 2
        assert len(record.extra_urls) == 2

    async def test_get_source_counts_is_batched(self, repository: NewsRepository) -> None:
        inserted = await repository.insert_items(
            [
                make_item("Bài một", url="https://coindesk.com/1/"),
                make_item("Bài hai", url="https://coindesk.com/2/"),
            ],
            now=NOW,
        )
        ids = [news_id for news_id, _ in inserted]
        assert await repository.get_source_counts(ids) == {ids[0]: 1, ids[1]: 1}
        await repository.link_source(
            ids[0], make_item("Bài một", source="decrypt", url="https://decrypt.co/news/1"), now=NOW
        )
        assert await repository.get_source_counts(ids) == {ids[0]: 2, ids[1]: 1}
        assert await repository.get_source_counts([]) == {}

    async def test_link_updates_published_at_to_earlier(self, repository: NewsRepository) -> None:
        (news_id, _), = await repository.insert_items(
            [make_item("Bài mới hơn", published_at=NOW)], now=NOW
        )
        await repository.link_source(
            news_id,
            make_item("Bài mới hơn", source="decrypt", url="https://decrypt.co/y",
                      published_at=NOW - timedelta(hours=6)),
            now=NOW,
        )
        record = await repository.get_record(news_id)
        assert record is not None
        assert record.published_at == NOW - timedelta(hours=6)

    async def test_link_sets_published_at_when_primary_is_missing(self, repository: NewsRepository) -> None:
        primary = make_item("Bài chưa có giờ đăng")
        primary.published_at = None
        (news_id, _), = await repository.insert_items([primary], now=NOW)
        await repository.link_source(
            news_id,
            make_item(
                "Bài chưa có giờ đăng",
                source="decrypt",
                url="https://decrypt.co/with-time",
                published_at=NOW - timedelta(hours=2),
            ),
            now=NOW,
        )
        record = await repository.get_record(news_id)
        assert record is not None
        assert record.published_at == NOW - timedelta(hours=2)


class TestReported:
    async def test_skip_unhandled_keeps_delivery_stats_honest(self, repository: NewsRepository) -> None:
        await repository.insert_items([make_item("Tin cũ không cần gửi")], now=NOW)
        assert await repository.count_unhandled() == 1
        assert await repository.skip_unhandled() == 1
        assert await repository.count_unhandled() == 0

        record = await repository.get_record(1)
        assert record is not None
        assert record.is_skipped is True
        assert record.is_reported is False
        assert await repository.is_reported(1) is True
        assert await repository.select_unreported(0.0, 24, 10, now=NOW) == []
        stats = await repository.get_stats()
        assert stats["reported"] == 0
        assert stats["skipped"] == 1
        assert stats["unreported"] == 0

    async def test_mark_reported_once(self, repository: NewsRepository) -> None:
        (news_id, _), = await repository.insert_items([make_item("Bài gửi đúng một lần")], now=NOW)
        assert await repository.mark_reported(news_id, message_id=555, chat_id=CHAT, now=NOW) is True
        # lần hai trả False -> Telegram không gửi trùng
        assert await repository.mark_reported(news_id, message_id=556, chat_id=CHAT, now=NOW) is False
        record = await repository.get_record(news_id)
        assert record is not None
        assert record.telegram_message_id == 555
        assert record.is_reported is True

    async def test_is_reported(self, repository: NewsRepository) -> None:
        (news_id, _), = await repository.insert_items([make_item("Một bài tin")], now=NOW)
        assert await repository.is_reported(news_id) is False
        await repository.mark_reported(news_id, message_id=1, chat_id=CHAT, now=NOW)
        assert await repository.is_reported(news_id) is True

    async def test_mark_reported_unknown_id(self, repository: NewsRepository) -> None:
        assert await repository.mark_reported(12345, message_id=1, chat_id=CHAT, now=NOW) is False

    async def test_failed_delivery_does_not_mark(self, repository: NewsRepository) -> None:
        (news_id, _), = await repository.insert_items([make_item("Gửi lỗi")], now=NOW)
        await repository.mark_report_failed(news_id, chat_id=CHAT, error="NetworkError", now=NOW)
        assert await repository.is_reported(news_id) is False

    async def test_retry_after_failure_succeeds(self, repository: NewsRepository) -> None:
        (news_id, _), = await repository.insert_items([make_item("Gửi lỗi rồi gửi lại")], now=NOW)
        await repository.mark_report_failed(news_id, chat_id=CHAT, error="NetworkError", now=NOW)
        assert await repository.mark_reported(news_id, message_id=9, chat_id=CHAT, now=NOW) is True


class TestQueries:
    @pytest.fixture
    async def seeded(self, repository: NewsRepository) -> NewsRepository:
        items = [
            make_item("OpenAI chính thức ra mắt GPT-5", url="https://techcrunch.com/a/",
                      published_at=NOW - timedelta(hours=1)),
            make_item("Ethereum ETF chính thức được duyệt", url="https://coindesk.com/a/",
                      category=Category.CRYPTO, published_at=NOW - timedelta(hours=2)),
            make_item("Tin cũ ngoài cửa sổ thời gian", url="https://decrypt.co/a/",
                      category=Category.CRYPTO, published_at=NOW - timedelta(days=10)),
        ]
        await repository.insert_items(items, now=NOW)
        return repository

    async def test_select_latest(self, seeded: NewsRepository) -> None:
        records = await seeded.select_latest(limit=10, now=NOW)
        assert len(records) == 3
        # sắp xếp mới nhất trước
        assert records[0].title.startswith("OpenAI")

    async def test_select_latest_by_category(self, seeded: NewsRepository) -> None:
        records = await seeded.select_latest(limit=10, category=Category.CRYPTO, now=NOW)
        assert [r.title for r in records] == [
            "Ethereum ETF chính thức được duyệt",
            "Tin cũ ngoài cửa sổ thời gian",
        ]

    async def test_select_latest_respects_limit(self, seeded: NewsRepository) -> None:
        assert len(await seeded.select_latest(limit=1, now=NOW)) == 1

    async def test_select_latest_only_unreported(self, seeded: NewsRepository) -> None:
        await seeded.mark_reported(1, message_id=1, chat_id=CHAT, now=NOW)
        records = await seeded.select_latest(limit=10, only_unreported=True, now=NOW)
        assert all(r.id != 1 for r in records)

    async def test_select_unreported_respects_min_score(self, seeded: NewsRepository) -> None:
        await seeded.update_trend_scores({1: 0.2, 2: 0.9, 3: 0.9})
        records = await seeded.select_unreported(min_score=0.5, max_age_hours=24, limit=10, now=NOW)
        assert {r.title for r in records} == {"Ethereum ETF chính thức được duyệt"}

    async def test_select_unreported_excludes_reported(self, seeded: NewsRepository) -> None:
        await seeded.mark_reported(1, message_id=1, chat_id=CHAT, now=NOW)
        records = await seeded.select_unreported(min_score=0.0, max_age_hours=24, limit=10, now=NOW)
        assert all(r.id != 1 for r in records)

    async def test_select_trending(self, seeded: NewsRepository) -> None:
        await seeded.update_trend_scores({1: 0.4, 2: 0.8, 3: 0.99})
        records = await seeded.select_trending(
            category=Category.CRYPTO, min_score=0.5, max_age_hours=24, limit=5, now=NOW
        )
        assert [r.title for r in records] == ["Ethereum ETF chính thức được duyệt"]

    async def test_update_trend_scores_returns_count(self, seeded: NewsRepository) -> None:
        assert await seeded.update_trend_scores({1: 0.4, 2: 0.8}) == 2

    async def test_get_record_missing(self, repository: NewsRepository) -> None:
        assert await repository.get_record(42) is None


class TestStatsAndRuns:
    async def test_counts(self, repository: NewsRepository) -> None:
        await repository.insert_items(
            [
                make_item("Bài AI một", url="https://techcrunch.com/1/"),
                make_item("Bài crypto một", url="https://coindesk.com/1/", category=Category.CRYPTO),
                make_item("Bài AI hai", url="https://techcrunch.com/2/"),
            ],
            now=NOW,
        )
        stats = await repository.get_stats()
        assert stats["total"] == 3
        assert stats["by_category"][Category.AI.value] == 2
        assert stats["by_category"][Category.CRYPTO.value] == 1
        assert stats["reported"] == 0
        assert stats["unreported"] == 3
        assert stats["last_run"] is None

    async def test_stats_on_empty_db(self, repository: NewsRepository) -> None:
        stats = await repository.get_stats()
        assert stats["total"] == 0
        assert stats["top_sources"] == []

    async def test_top_sources_counts(self, repository: NewsRepository) -> None:
        await repository.insert_items(
            [
                make_item("Bài A", source="coindesk"),
                make_item("Bài B", source="coindesk"),
                make_item("Bài C", source="decrypt"),
            ],
            now=NOW,
        )
        top = (await repository.get_stats())["top_sources"]
        assert top[0] == ("coindesk", 2)
        assert top[1] == ("decrypt", 1)

    async def test_crawl_runs(self, repository: NewsRepository) -> None:
        from app.database.models import CrawlStats

        run_id = await repository.start_run(now=NOW)
        stats = CrawlStats(
            run_id=run_id,
            started_at=NOW,
            sources_total=12,
            articles_found=40,
            new_articles=35,
        )
        await repository.finish_run(stats)
        runs = await repository.list_runs(limit=5)
        assert len(runs) == 1
        assert runs[0]["id"] == run_id
        assert runs[0]["articles_found"] == 40
        assert runs[0]["new_articles"] == 35
