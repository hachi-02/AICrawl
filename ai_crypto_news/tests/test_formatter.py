"""Test định dạng message Telegram (plain text + emoji)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.database.models import Category, NewsRecord
from app.telegram import formatter
from tests.conftest import NOW


def record(news_id: int = 1, **overrides) -> NewsRecord:
    data: dict = {
        "id": news_id,
        "title": f"OpenAI ra mắt GPT-5 {news_id}",
        "url": f"https://techcrunch.com/{news_id}",
        "source": "techcrunch",
        "category": Category.AI,
        "published_at": NOW - timedelta(hours=2),
        "summary": "Mô hình mới với khả năng suy luận tốt hơn.",
        "source_count": 2,
        "trend_score": 0.82,
    }
    data.update(overrides)
    return NewsRecord(**data)


class TestRelativeTime:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(seconds=30), "vừa xong"),
            (timedelta(minutes=5), "5 phút trước"),
            (timedelta(hours=3), "3 giờ trước"),
            (timedelta(days=1), "1 ngày trước"),
            (timedelta(days=3), "3 ngày trước"),
        ],
    )
    def test_relative_time(self, delta: timedelta, expected: str) -> None:
        assert formatter.relative_time(NOW - delta, now=NOW) == expected

    def test_relative_time_no_timestamp(self) -> None:
        assert formatter.relative_time(None, now=NOW) == "vừa xong"

    def test_relative_time_future_is_clamped(self) -> None:
        assert formatter.relative_time(NOW + timedelta(hours=5), now=NOW) == "vừa xong"


class TestFormatTrending:
    def test_contains_all_required_parts(self) -> None:
        text = formatter.format_trending(Category.AI, [record()], NOW)
        assert "AI" in text
        assert "OpenAI ra mắt GPT-5 1" in text
        assert "techcrunch" in text
        assert "https://techcrunch.com/1" in text
        assert "2 giờ trước" in text

    def test_numbered_and_sorted(self) -> None:
        text = formatter.format_trending(
            Category.AI, [record(1, trend_score=0.4), record(2, trend_score=0.95)], NOW
        )
        assert text.index("GPT-5 2") < text.index("GPT-5 1")
        assert "2." in text and "1." in text

    def test_sort_works_without_any_timestamp(self) -> None:
        """Không có tin nào có published_at vẫn phải sort được (không so sánh naive/aware)."""
        records = [
            NewsRecord(id=1, title="Không có thời gian", url="u1", source="s",
                       category=Category.AI, trend_score=0.5),
            NewsRecord(id=2, title="Có thời gian", url="u2", source="s",
                       category=Category.AI, trend_score=0.5, published_at=NOW),
        ]
        text = formatter.format_trending(Category.AI, records, NOW)
        assert text.index("Có thời gian") < text.index("Không có thời gian")

    def test_shows_extra_source_count(self) -> None:
        text = formatter.format_trending(Category.AI, [record(source_count=3)], NOW)
        assert "3 nguồn" in text or "+2" in text

    def test_empty_list(self) -> None:
        assert formatter.format_trending(Category.AI, [], NOW).strip() != ""

    def test_plain_text_no_markup(self) -> None:
        text = formatter.format_trending(Category.AI, [record()], NOW)
        assert "*" not in text
        assert "_" not in text
        assert "<" not in text


class TestSanitize:
    def test_removes_control_chars(self) -> None:
        assert "\x00" not in formatter.sanitize("a\x00b\x07c")
        assert formatter.sanitize("a\x00b\x07c") == "a b c"

    def test_keeps_vietnamese(self) -> None:
        assert formatter.sanitize("Bài viết tiếng Việt") == "Bài viết tiếng Việt"

    def test_no_markup_helpers_needed(self) -> None:
        # plain text: không escape markdown, chỉ loại ký tự điều khiển
        assert formatter.sanitize("a_b*c") == "a_b*c"

    def test_collapse_spaces(self) -> None:
        assert formatter.sanitize("a     b") == "a b"


class TestSplitMessage:
    def test_short_text_stays_one_chunk(self) -> None:
        assert formatter.split_message("ngắn", 100) == ["ngắn"]

    def test_splits_on_paragraphs(self) -> None:
        text = ("A" * 60 + "\n\n") * 5
        chunks = formatter.split_message(text, 150)
        assert len(chunks) > 1
        assert all(len(chunk) <= 150 for chunk in chunks)
        assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")

    def test_hard_split_when_no_break_point(self) -> None:
        chunks = formatter.split_message("B" * 500, 100)
        assert len(chunks) == 5
        assert all(len(chunk) <= 100 for chunk in chunks)

    def test_empty_text(self) -> None:
        assert formatter.split_message("", 100) == []

    def test_never_exceeds_limit_with_newlines(self) -> None:
        text = "\n".join(["x" * 50] * 40)
        for chunk in formatter.split_message(text, 120):
            assert len(chunk) <= 120


class TestFormatLatestAndStatus:
    def test_format_latest(self) -> None:
        text = formatter.format_latest([record(), record(2, category=Category.CRYPTO)], NOW)
        assert "GPT-5 1" in text and "GPT-5 2" in text

    def test_format_latest_empty(self) -> None:
        assert formatter.format_latest([], NOW).strip() != ""

    def test_format_status_keys(self) -> None:
        stats = {
            "total": 120,
            "reported": 80,
            "unreported": 40,
            "by_category": {"AI": 70, "CRYPTO": 50},
            "last_24h": 12,
            "source_mentions": 140,
            "extra_sources": 20,
            "last_run": None,
            "top_sources": [("coindesk", 40), ("techcrunch", 30)],
            "db_size_bytes": 1024,
        }
        text = formatter.format_status(stats, {}, NOW)
        assert "120" in text
        assert "coindesk" in text
        assert "AI" in text and "CRYPTO" in text

    def test_format_status_handles_missing_keys(self) -> None:
        assert formatter.format_status({}, {}, NOW).strip() != ""

    def test_format_status_with_last_run(self) -> None:
        stats = {
            "total": 1, "reported": 0, "unreported": 1,
            "by_category": {"AI": 1, "CRYPTO": 0}, "last_24h": 1,
            "source_mentions": 1, "extra_sources": 0,
            "last_run": {
                "id": 7, "started_at": "2026-09-29T10:00:00+00:00",
                "articles_found": 30, "new_articles": 25, "telegram_sent": 3,
            },
            "top_sources": [], "db_size_bytes": 10,
        }
        text = formatter.format_status(stats, {}, NOW)
        assert "30" in text
