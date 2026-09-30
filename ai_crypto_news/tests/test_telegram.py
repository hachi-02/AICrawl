"""Test Telegram: quan trọng nhất là **không gửi trùng tin đã báo**.

Bot Telegram thật được thay bằng ``FakeBot`` nên test chạy nhanh, không cần
token và không gọi ra ngoài mạng.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from app.config import Settings
from app.database.models import Category, NewsRecord
from app.database.repository import NewsRepository
from app.telegram.bot import NewsTelegramBot, TelegramNotifier
from tests.conftest import NOW, make_item


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    """Thay thế ``telegram.Bot`` chỉ cần đúng hai phương thức notifier dùng."""

    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[str] = []
        self.calls = 0
        self._fail_times = fail_times
        self._next_id = 100
        self.closed = False

    async def send_message(self, text: str, **kwargs: Any) -> FakeMessage:
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("Telegram lỗi giả lập")
        self.sent.append(text)
        self._next_id += 1
        return FakeMessage(self._next_id)

    async def shutdown(self) -> None:
        self.closed = True


class FailAfterFirstBot(FakeBot):
    """Gửi chunk đầu thành công, mọi chunk sau đều lỗi."""

    async def send_message(self, text: str, **kwargs: Any) -> FakeMessage:
        if self.calls >= 1:
            self.calls += 1
            raise RuntimeError("Telegram lỗi sau chunk đầu")
        return await super().send_message(text, **kwargs)


def make_record(news_id: int = 1, category: Category = Category.AI, **overrides: Any) -> NewsRecord:
    data: dict[str, Any] = {
        "id": news_id,
        "title": f"Tin số {news_id} về AI",
        "url": f"https://techcrunch.com/{news_id}",
        "source": "techcrunch",
        "category": category,
        "published_at": NOW - timedelta(hours=1),
        "summary": "Tóm tắt ngắn",
        "source_count": 1,
        "trend_score": 0.8,
    }
    data.update(overrides)
    return NewsRecord(**data)


@pytest.fixture
def notifier_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "test.db",
        telegram_enabled=True,
        telegram_bot_token="123:abc",
        telegram_chat_id="@news",
        telegram_rate_limit_seconds=0.0,
    )


def build_notifier(
    repository: NewsRepository, settings: Settings, fail_times: int = 0
) -> tuple[TelegramNotifier, FakeBot]:
    bot = FakeBot(fail_times=fail_times)
    return TelegramNotifier(bot, settings.telegram_chat_id, settings, repository), bot  # type: ignore[arg-type]


class TestNoDuplicateDelivery:
    async def test_sends_once_then_nothing(
        self, repository: NewsRepository, notifier_settings: Settings
    ) -> None:
        (news_id, _), = await repository.insert_items(
            [make_item("OpenAI chính thức ra mắt GPT-5")],
            now=NOW,
        )
        notifier, bot = build_notifier(repository, notifier_settings)
        records = await repository.select_unreported(min_score=0.0, max_age_hours=24, limit=5, now=NOW)

        sent, failed = await notifier.send_trending(records, now=NOW)
        assert (sent, failed) == (1, 0)
        assert len(bot.sent) == 1

        # lần hai: không được gửi lại
        sent2, failed2 = await notifier.send_trending(records, now=NOW)
        assert (sent2, failed2) == (0, 0)
        assert len(bot.sent) == 1
        assert await repository.is_reported(news_id) is True

    async def test_stale_record_object_still_not_resent(
        self, repository: NewsRepository, notifier_settings: Settings
    ) -> None:
        """Object record đã cũ (is_reported=False) vẫn không được gửi lại."""
        (news_id, _), = await repository.insert_items(
            [make_item("Một bài tin AI hay")],
            now=NOW,
        )
        notifier, bot = build_notifier(repository, notifier_settings)
        stale = [make_record(news_id, is_reported=False)]

        assert await notifier.send_trending(stale, now=NOW) == (1, 0)
        assert await notifier.send_trending(stale, now=NOW) == (0, 0)
        assert len(bot.sent) == 1

    async def test_concurrent_calls_send_once(
        self, repository: NewsRepository, notifier_settings: Settings
    ) -> None:
        (news_id, _), = await repository.insert_items(
            [make_item("Bài tin cạnh tranh gửi")],
            now=NOW,
        )
        notifier, bot = build_notifier(repository, notifier_settings)
        records = await repository.select_unreported(min_score=0.0, max_age_hours=24, limit=5, now=NOW)

        results = await asyncio.gather(
            notifier.send_trending(records, now=NOW),
            notifier.send_trending(records, now=NOW),
            notifier.send_trending(records, now=NOW),
        )
        assert sum(sent for sent, _ in results) == 1
        assert len(bot.sent) == 1


class TestFailures:
    async def test_failure_does_not_mark_and_retries(
        self, repository: NewsRepository, notifier_settings: Settings
    ) -> None:
        (news_id, _), = await repository.insert_items(
            [make_item("Bài gửi lỗi rồi gửi lại")],
            now=NOW,
        )
        notifier, bot = build_notifier(repository, notifier_settings, fail_times=99)
        records = await repository.select_unreported(min_score=0.0, max_age_hours=24, limit=5, now=NOW)

        sent, failed = await notifier.send_trending(records, now=NOW)
        assert (sent, failed) == (0, 1)
        assert await repository.is_reported(news_id) is False

        # lần sau bot hoạt động lại -> phải gửi được
        bot._fail_times = 0
        sent, failed = await notifier.send_trending(records, now=NOW)
        assert (sent, failed) == (1, 0)
        assert await repository.is_reported(news_id) is True

    async def test_empty_records_is_noop(
        self, repository: NewsRepository, notifier_settings: Settings
    ) -> None:
        notifier, bot = build_notifier(repository, notifier_settings)
        assert await notifier.send_trending([], now=NOW) == (0, 0)
        assert bot.calls == 0

    async def test_shutdown_closes_bot(
        self, repository: NewsRepository, notifier_settings: Settings
    ) -> None:
        notifier, bot = build_notifier(repository, notifier_settings)
        await notifier.shutdown()
        assert bot.closed is True

    async def test_partial_multi_chunk_send_is_not_marked_reported(
        self,
        repository: NewsRepository,
        notifier_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Chunk đầu thành công nhưng chunk sau lỗi thì cả nhóm phải được retry."""
        (news_id, _), = await repository.insert_items(
            [make_item("Bài Telegram dài cần chia chunk")],
            now=NOW,
        )
        bot = FailAfterFirstBot()
        notifier = TelegramNotifier(
            bot, notifier_settings.telegram_chat_id, notifier_settings, repository  # type: ignore[arg-type]
        )
        monkeypatch.setattr("app.telegram.bot.split_message", lambda _text, _limit: ["phần 1", "phần 2"])
        records = await repository.select_unreported(min_score=0.0, max_age_hours=24, limit=5, now=NOW)

        assert await notifier.send_trending(records, now=NOW) == (0, 1)
        assert bot.sent == ["phần 1"]
        assert await repository.is_reported(news_id) is False


class TestGrouping:
    async def test_sends_one_message_per_category(
        self, repository: NewsRepository, notifier_settings: Settings
    ) -> None:
        await repository.insert_items(
            [
                make_item("Tin AI một", url="https://techcrunch.com/a1/"),
                make_item("Tin AI hai", url="https://techcrunch.com/a2/"),
                make_item("Tin crypto một", url="https://coindesk.com/c1/", category=Category.CRYPTO),
            ],
            now=NOW,
        )
        notifier, bot = build_notifier(repository, notifier_settings)
        records = await repository.select_unreported(min_score=0.0, max_age_hours=24, limit=10, now=NOW)

        sent, failed = await notifier.send_trending(records, now=NOW)
        assert (sent, failed) == (3, 0)
        assert len(bot.sent) == 2  # 1 message cho AI, 1 cho CRYPTO
        assert any("AI" in text for text in bot.sent)
        assert any("CRYPTO" in text.upper() for text in bot.sent)

    async def test_retry_exhausted_gives_up(
        self, repository: NewsRepository, tmp_path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "t.db",
            telegram_enabled=True,
            telegram_bot_token="123:abc",
            telegram_chat_id="@news",
            telegram_rate_limit_seconds=0.0,
            telegram_max_retries=2,
            telegram_retry_base_delay=0.1,
        )
        notifier, bot = build_notifier(repository, settings, fail_times=99)
        records = [make_record(1)]
        # chưa có trong DB -> mark_reported trả False nhưng không ném lỗi
        sent, failed = await notifier.send_trending(records, now=NOW)
        assert (sent, failed) == (0, 1)
        # telegram_max_retries = số lần thử tối đa cho mỗi chunk
        assert bot.calls == settings.telegram_max_retries


class TestApplication:
    async def test_build_includes_job_queue(
        self, repository: NewsRepository, notifier_settings: Settings
    ) -> None:
        async def run_crawl(_reason: str):
            raise AssertionError("Không được chạy crawl khi chỉ build application")

        telegram = NewsTelegramBot(notifier_settings, repository, run_crawl)
        application = telegram.build()
        assert application.job_queue is not None
