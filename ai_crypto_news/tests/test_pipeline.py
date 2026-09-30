"""Test tích hợp các bước lưu, liên kết nguồn và tính trend của pipeline."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from app.config import Settings
from app.crawler.browser import BrowserManager
from app.crawler.sources import CrawlOutcome
from app.database.models import Category, CrawlStats
from app.database.repository import NewsRepository
from app.main import Pipeline
from tests.conftest import NOW, make_item


class TestPipelineTrend:
    async def test_new_story_uses_linked_source_count_for_trend(
        self, repository: NewsRepository, settings: Settings
    ) -> None:
        pipeline = Pipeline(settings, repository, BrowserManager(settings))
        items = [
            make_item(
                "OpenAI releases GPT-5 model to all users",
                source="techcrunch",
                url="https://techcrunch.com/2026/09/29/gpt-5/",
            ),
            make_item(
                "OpenAI releases GPT-5 model to all users!",
                source="venturebeat",
                url="https://venturebeat.com/ai/openai-gpt-5/",
            ),
        ]

        new_items, new_ids, linked, linked_ids = await pipeline._deduplicate_and_store(items, NOW)
        assert len(new_items) == 1
        assert linked == 1
        assert linked_ids == new_ids
        await pipeline._apply_trend_scores(list(dict.fromkeys([*new_ids, *linked_ids])), NOW)

        record = await repository.get_record(new_ids[0])
        assert record is not None
        assert record.source_count == 2
        expected = pipeline.analyzer.score_item(
            new_items[0],
            NOW,
            topic_count=1,
            source_count=2,
        ).total
        assert record.trend_score == pytest.approx(expected, abs=1e-6)

    async def test_existing_story_is_rescored_after_linking_a_new_source(
        self, repository: NewsRepository, settings: Settings
    ) -> None:
        pipeline = Pipeline(settings, repository, BrowserManager(settings))
        primary = make_item(
            "OpenAI releases GPT-5 model to all users",
            source="techcrunch",
            url="https://techcrunch.com/primary/",
        )
        (news_id, _), = await repository.insert_items([primary], now=NOW)
        await pipeline._apply_trend_scores([news_id], NOW)
        before = await repository.get_record(news_id)
        assert before is not None

        duplicate = make_item(
            "OpenAI releases GPT-5 model to all users!",
            source="venturebeat",
            url="https://venturebeat.com/secondary/",
        )
        _, _, linked, linked_ids = await pipeline._deduplicate_and_store([duplicate], NOW)
        assert linked == 1
        await pipeline._apply_trend_scores(linked_ids, NOW)

        after = await repository.get_record(news_id)
        assert after is not None
        assert after.source_count == 2
        assert after.trend_score > before.trend_score

    async def test_broadcast_reserves_limit_for_each_category(
        self, repository: NewsRepository, settings: Settings
    ) -> None:
        class CaptureNotifier:
            def __init__(self) -> None:
                self.records = []

            async def send_trending(self, records, now):  # type: ignore[no-untyped-def]
                self.records = list(records)
                return len(records), 0

        crypto = [
            make_item(
                f"Bitcoin market update number {index}",
                source="coindesk",
                url=f"https://coindesk.com/{index}/",
                category=Category.CRYPTO,
            )
            for index in range(12)
        ]
        ai = make_item("OpenAI launches a major model", url="https://techcrunch.com/ai/")
        inserted = await repository.insert_items([*crypto, ai], now=NOW)
        await repository.update_trend_scores({news_id: 0.9 for news_id, _ in inserted})

        notifier = CaptureNotifier()
        pipeline = Pipeline(settings, repository, BrowserManager(settings), notifier=notifier)  # type: ignore[arg-type]
        stats = CrawlStats()
        await pipeline._broadcast(NOW, stats)

        categories = [record.category for record in notifier.records]
        assert categories.count(Category.CRYPTO) == settings.trend_max_per_category
        assert categories.count(Category.AI) == 1

    async def test_failed_post_crawl_step_still_finishes_run(
        self,
        repository: NewsRepository,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        browser = BrowserManager(settings)
        pipeline = Pipeline(settings, repository, browser)
        monkeypatch.setattr(browser, "ensure_alive", AsyncMock())
        monkeypatch.setattr(
            pipeline.crawler,
            "crawl_sources",
            AsyncMock(return_value=CrawlOutcome(items=[make_item("Tin gây lỗi pipeline")], total_articles=1)),
        )
        monkeypatch.setattr(
            pipeline,
            "_deduplicate_and_store",
            AsyncMock(side_effect=RuntimeError("dedup failed")),
        )

        with pytest.raises(RuntimeError, match="dedup failed"):
            await pipeline.run_once(reason="test")

        runs = await repository.list_runs()
        assert runs[0]["finished_at"] is not None
