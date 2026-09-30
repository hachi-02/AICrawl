"""Test tích hợp các bước lưu, liên kết nguồn và tính trend của pipeline."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.crawler.browser import BrowserManager
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

        new_items, new_ids, linked = await pipeline._deduplicate_and_store(items, NOW)
        assert len(new_items) == 1
        assert linked == 1
        await pipeline._apply_trend_scores(new_ids, new_items, NOW)

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
