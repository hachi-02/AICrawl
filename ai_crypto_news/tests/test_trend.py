"""Test công thức trend và bộ chấm điểm."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import Settings
from app.database.models import Category, NewsRecord
from app.trend.analyzer import (
    TrendAnalyzer,
    combine,
    freshness_score,
    keyword_score,
    source_count_score,
    topic_volume_score,
)
from tests.conftest import NOW, make_item

HALF_LIFE = Settings(_env_file=None).trend_freshness_half_life_hours


@pytest.fixture
def analyzer(settings: Settings) -> TrendAnalyzer:
    return TrendAnalyzer(settings)


class TestComponents:
    def test_freshness_decays_with_age(self) -> None:
        scores = [
            freshness_score(NOW - timedelta(hours=age), NOW, HALF_LIFE)
            for age in (0, 2, 6, 12, 24, 48)
        ]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] == pytest.approx(1.0)
        # sau 24 giờ điểm phải nhỏ (giảm mạnh), không phải 0 tuyệt đối
        assert scores[4] < 0.35

    def test_freshness_half_life_halves(self) -> None:
        base = freshness_score(NOW, NOW, HALF_LIFE)
        later = freshness_score(NOW - timedelta(hours=HALF_LIFE), NOW, HALF_LIFE)
        assert later == pytest.approx(base / 2, abs=0.01)

    def test_freshness_never_negative(self) -> None:
        assert freshness_score(NOW - timedelta(days=90), NOW, HALF_LIFE) >= 0.0

    def test_freshness_future_timestamp(self) -> None:
        assert freshness_score(NOW + timedelta(hours=2), NOW, HALF_LIFE) == 1.0

    def test_freshness_without_timestamp_uses_middle(self) -> None:
        score = freshness_score(None, NOW, HALF_LIFE)
        assert 0.0 < score < 1.0

    def test_keyword_score_ai(self) -> None:
        assert keyword_score("OpenAI releases GPT-5 model", Category.AI) > 0.5
        assert keyword_score("Bitcoin ETF approved by SEC", Category.AI) < 0.3

    def test_keyword_score_crypto(self) -> None:
        assert keyword_score("Bitcoin ETF approved by SEC", Category.CRYPTO) > 0.5
        assert keyword_score("Local coffee shop opens downtown", Category.CRYPTO) == 0.0

    def test_keyword_score_is_case_insensitive(self) -> None:
        assert keyword_score("openai AND anthropic", Category.AI) > 0

    def test_keyword_score_bounded(self) -> None:
        text = "OpenAI Anthropic Google DeepMind Meta AI Llama Mistral Nvidia AI model AGI"
        assert 0.0 <= keyword_score(text, Category.AI) <= 1.0

    def test_source_count_score(self) -> None:
        # 1 nguồn = 0, từ 4 nguồn trở lên = 1
        assert source_count_score(1) == 0.0
        assert source_count_score(2) == pytest.approx(1 / 3)
        assert source_count_score(3) == pytest.approx(2 / 3)
        assert source_count_score(4) == 1.0
        assert source_count_score(9) == 1.0

    def test_topic_volume_score(self) -> None:
        assert topic_volume_score(0) == 0.0
        assert topic_volume_score(1) == 0.0
        assert topic_volume_score(10**6) == 1.0
        assert 0 < topic_volume_score(5) < 1

    def test_combine_weights_and_bounds(self) -> None:
        assert combine(1.0, 1.0, 1.0, 1.0) == pytest.approx(1.0)
        assert combine(0.0, 0.0, 0.0, 0.0) == 0.0
        # trọng số cộng lại <= 1 nên không vượt ngưỡng
        assert 0.0 <= combine(0.5, 0.5, 0.5, 0.5, 0.5) <= 1.0

    def test_freshness_dominates_more_than_keyword(self) -> None:
        assert combine(1.0, 0.0, 0.0, 0.0) > combine(0.0, 1.0, 0.0, 0.0)


class TestScoreItem:
    def test_total_is_bounded(self, analyzer: TrendAnalyzer) -> None:
        hot = make_item("OpenAI and Anthropic both release GPT-5 AI models", category=Category.AI)
        cold = make_item("Một bài viết bình thường không có từ khoá", category=Category.AI)
        for item, topic in ((hot, 20), (cold, 1)):
            assert 0.0 <= analyzer.score_item(item, NOW, topic_count=topic).total <= 1.0

    def test_hot_beats_cold(self, analyzer: TrendAnalyzer) -> None:
        hot = make_item("OpenAI releases GPT-5 with new reasoning model", category=Category.AI)
        cold = make_item("Café mới mở cửa ở quận ba", category=Category.AI)
        assert analyzer.score_item(hot, NOW, topic_count=10).total > analyzer.score_item(
            cold, NOW, topic_count=1
        ).total

    def test_freshness_boosts(self, analyzer: TrendAnalyzer) -> None:
        title = "OpenAI releases GPT-5 model"
        fresh = make_item(title, published_at=NOW - timedelta(minutes=10))
        old = make_item(title, published_at=NOW - timedelta(hours=20))
        assert analyzer.score_item(fresh, NOW).total > analyzer.score_item(old, NOW).total

    def test_source_count_boosts(self, analyzer: TrendAnalyzer) -> None:
        item = make_item("Bitcoin ETF approved")
        assert analyzer.score_item(item, NOW, source_count=3).total > analyzer.score_item(
            item, NOW, source_count=1
        ).total

    def test_components_dict(self, analyzer: TrendAnalyzer) -> None:
        components = analyzer.score_item(make_item("Bitcoin ETF news"), NOW, topic_count=3)
        data = components.as_dict()
        assert set(data) == {"freshness", "keyword", "source_count", "topic_volume", "engagement", "total"}
        assert data["total"] == round(components.total, 4)

    def test_is_trending_uses_setting(self, settings: Settings) -> None:
        analyzer = TrendAnalyzer(settings)
        assert analyzer.is_trending(settings.trend_min_score) is True
        assert analyzer.is_trending(settings.trend_min_score - 0.01) is False


class TestBatch:
    def test_score_batch_returns_aligned_list(self, analyzer: TrendAnalyzer) -> None:
        items = [make_item("OpenAI ships GPT-5 model"), make_item("Một bài bình thường")]
        scored = analyzer.score_batch(items, now=NOW)
        assert len(scored) == len(items)
        assert scored[0].total > scored[1].total

    def test_score_batch_empty(self, analyzer: TrendAnalyzer) -> None:
        assert analyzer.score_batch([], now=NOW) == []

    def test_score_record_uses_stored_source_count(self, analyzer: TrendAnalyzer) -> None:
        record = NewsRecord(
            id=1,
            title="Bitcoin ETF được duyệt",
            url="https://coindesk.com/1",
            source="coindesk",
            category=Category.CRYPTO,
            published_at=NOW - timedelta(hours=1),
            source_count=4,
        )
        assert analyzer.score_record(record, now=NOW).source_count == 1.0


def test_newsitem_enrich_is_idempotent() -> None:
    from app.database.models import NewsItem

    item: NewsItem = NewsItem(
        title="  OpenAI   releases GPT-5  ",
        url="https://techcrunch.com/x?utm_source=feed",
        source="TechCrunch",
        category=Category.AI,
        summary="Bài viết về <b>AI</b> & mô hình mới",
    )
    first = item.enrich()
    second = item.enrich()
    assert first == second
    assert item.canonical_url == "https://techcrunch.com/x"
    assert "openai" in item.title_tokens
    assert item.content_hash
    assert item.simhash != 0
