"""Fixture dùng chung cho toàn bộ test."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.database.database import Database
from app.database.models import Category, NewsItem
from app.database.repository import NewsRepository

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def now() -> datetime:
    """Mốc thời gian cố định để test không phụ thuộc thực tế."""
    return NOW


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Cấu hình test: database tạm, không cần robot, không Telegram."""
    return Settings(
        _env_file=None,
        database_path=tmp_path / "test.db",
        log_file=tmp_path / "logs" / "test.log",
        respect_robots_txt=False,
        telegram_enabled=False,
        telegram_bot_token="",
        telegram_chat_id="",
        request_delay_seconds=0.0,
        retry_backoff_seconds=0.0,
        trend_topic_window_minutes=60,
    )


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(":memory:")
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def repository(database: Database) -> NewsRepository:
    repo = NewsRepository(database)
    await repo.initialize()
    return repo


def make_item(
    title: str,
    source: str = "techcrunch",
    url: str | None = None,
    category: Category = Category.AI,
    summary: str | None = None,
    published_at: datetime | None = None,
    author: str | None = None,
) -> NewsItem:
    """Tạo NewsItem đã enrich() sẵn."""
    slug = title.lower().replace(" ", "-")[:60]
    item = NewsItem(
        title=title,
        url=url or f"https://techcrunch.com/2026/09/29/{slug}/",
        source=source,
        category=category,
        summary=summary,
        author=author,
        published_at=published_at if published_at is not None else NOW - timedelta(hours=1),
    )
    return item.enrich()


@pytest.fixture
def make_news_item() -> Iterator[object]:
    return make_item
