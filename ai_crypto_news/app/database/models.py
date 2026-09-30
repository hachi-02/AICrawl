"""Pydantic/dataclass models dùng chung cho toàn pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.dedupe.normalizer import (
    canonicalize_url,
    content_hash,
    fingerprint_features,
    parse_datetime,
    simhash,
    simint,
    token_set,
    to_iso,
)


class Category(StrEnum):
    """Chủ đề của bài tin."""

    AI = "AI"
    CRYPTO = "CRYPTO"

    @property
    def label(self) -> str:
        return "AI" if self is Category.AI else "CRYPTO"


CATEGORY_VALUES: tuple[str, ...] = tuple(category.value for category in Category)


class NewsItem(BaseModel):
    """Một bài tin sau khi crawler parse xong (chưa qua dedup)."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=False)

    title: str = Field(min_length=3, max_length=1000)
    url: str = Field(min_length=8)
    source: str = Field(min_length=1, max_length=100)
    category: Category
    author: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    image_url: str | None = None
    content: str | None = None

    canonical_url: str = ""
    content_hash: str = ""
    simhash: int = 0
    title_tokens: frozenset[str] = frozenset()

    @field_validator("published_at", mode="before")
    @classmethod
    def _parse_published_at(cls, value: object) -> datetime | None:
        return parse_datetime(value)

    @field_validator("author", "summary", "image_url", "content", mode="before")
    @classmethod
    def _clean_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in {"null", "none", "undefined", "n/a"}:
            return None
        return text

    def enrich(self) -> NewsItem:
        """Tính các trường phái sinh phục vụ dedup (canonical_url, hash, simhash)."""
        self.canonical_url = canonicalize_url(self.url) or self.url
        self.content_hash = content_hash(self.title, self.source)
        self.simhash = simhash(fingerprint_features(self.title, self.summary, self.content))
        self.title_tokens = token_set(self.title)
        return self

    @property
    def text_for_trend(self) -> str:
        """Nội dung dùng để tính trend_score."""
        return f"{self.title} {self.summary or ''}"


class NewsRecord(BaseModel):
    """Một dòng trong bảng ``news`` đã đọc từ SQLite."""

    model_config = ConfigDict(frozen=True)

    id: int
    title: str
    url: str
    source: str
    category: Category
    author: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    image_url: str | None = None
    content_hash: str = ""
    canonical_url: str = ""
    simhash: int = 0
    source_count: int = 1
    trend_score: float = 0.0
    is_reported: bool = False
    is_skipped: bool = False
    reported_at: datetime | None = None
    telegram_message_id: int | None = None
    first_seen_at: datetime | None = None
    crawled_at: datetime | None = None
    extra_urls: list[str] = Field(default_factory=list)


class SourceOutcome(BaseModel):
    """Kết quả crawl của một nguồn tin."""

    model_config = ConfigDict(frozen=True)

    source: str
    category: Category
    items: list[NewsItem] = Field(default_factory=list)
    error: str | None = None
    attempts: int = 1
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class CrawlStats:
    """Thống kê một vòng crawl, dùng cho log và bảng ``crawl_runs``."""

    run_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    sources_total: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    articles_found: int = 0
    new_articles: int = 0
    duplicates: int = 0
    extra_sources_linked: int = 0
    telegram_sent: int = 0
    telegram_failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": to_iso(self.started_at),
            "finished_at": to_iso(self.finished_at),
            "sources_total": self.sources_total,
            "sources_ok": self.sources_ok,
            "sources_failed": self.sources_failed,
            "articles_found": self.articles_found,
            "new_articles": self.new_articles,
            "duplicates": self.duplicates,
            "extra_sources_linked": self.extra_sources_linked,
            "telegram_sent": self.telegram_sent,
            "telegram_failed": self.telegram_failed,
            "errors": list(self.errors),
        }


@dataclass(slots=True)
class TrendComponents:
    """Các thành phần điểm trend và tổng điểm (0 → 1)."""

    freshness: float = 0.0
    keyword: float = 0.0
    source_count: float = 0.0
    topic_volume: float = 0.0
    engagement: float = 0.0
    total: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "freshness": round(self.freshness, 4),
            "keyword": round(self.keyword, 4),
            "source_count": round(self.source_count, 4),
            "topic_volume": round(self.topic_volume, 4),
            "engagement": round(self.engagement, 4),
            "total": round(self.total, 4),
        }


def row_to_record(row: Any, extra_urls: list[str] | None = None) -> NewsRecord:
    """Chuyển một ``sqlite3.Row`` của bảng ``news`` thành :class:`NewsRecord`."""
    data = dict(row)
    data.pop("extra_urls", None)
    data["is_reported"] = bool(data.get("is_reported"))
    data["is_skipped"] = bool(data.get("is_skipped"))
    data["category"] = Category(data["category"])
    data["published_at"] = parse_datetime(data.get("published_at"))
    data["reported_at"] = parse_datetime(data.get("reported_at"))
    data["first_seen_at"] = parse_datetime(data.get("first_seen_at"))
    data["crawled_at"] = parse_datetime(data.get("crawled_at"))
    # simhash lưu dạng TEXT (16 ký tự hex) nên phải giải mã, không int() thẳng.
    data["simhash"] = simint(data.get("simhash") or 0)
    data["source_count"] = int(data.get("source_count") or 1)
    data["trend_score"] = float(data.get("trend_score") or 0.0)
    return NewsRecord(**data, extra_urls=extra_urls or [])
