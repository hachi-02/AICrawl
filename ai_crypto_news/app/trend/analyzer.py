"""Tính trend_score cho mỗi bản tin.

Phiên bản đầu tiên dùng 4 thành phần, không cần LLM::

    trend = 0.35 * freshness
          + 0.30 * keyword
          + 0.20 * source_count
          + 0.15 * topic_volume

``engagement`` được tách riêng và bằng 0 ở v1, chỗ này dành cho dữ liệu
tương tác công khai (lượt xem/bình luận) nếu sau này có thu thập được.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

from app.config import Settings
from app.database.models import Category, NewsItem, NewsRecord, TrendComponents
from app.dedupe.normalizer import (
    hours_since,
    normalize_title,
    overlap_coefficient,
    token_set,
    utcnow,
)
from app.logging_config import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# Từ khóa theo chủ đề (trọng số 0 → 1)
# --------------------------------------------------------------------------

AI_KEYWORDS: dict[str, float] = {
    "openai": 1.0, "chatgpt": 0.95, "gpt-5": 1.0, "gpt-4": 0.8, "o1": 0.6, "o3": 0.7,
    "anthropic": 1.0, "claude": 0.9, "gemini": 0.9, "deepmind": 0.85, "grok": 0.8,
    "llama": 0.75, "mistral": 0.7, "hugging face": 0.7, "perplexity": 0.65,
    "nvidia": 0.9, "amd": 0.6, "tsmc": 0.7, "broadcom": 0.65, "asml": 0.65,
    "copilot": 0.75, "midjourney": 0.7, "sora": 0.75, "dall-e": 0.6, "stable diffusion": 0.6,
    "artificial intelligence": 0.6, "machine learning": 0.55, "neural": 0.5, "transformer": 0.5,
    "llm": 0.8, "large language model": 0.75, "ai model": 0.7, "ai agent": 0.8, "agentic": 0.75,
    "ai act": 0.85, "ai regulation": 0.8, "ai safety": 0.7, "alignment": 0.55, "agi": 0.95,
    "superintelligence": 0.9, "data center": 0.6, "gpu": 0.6, "inference": 0.5,
    "open source model": 0.65, "benchmark": 0.45, "startup": 0.4, "funding": 0.45,
    "acquisition": 0.5, "layoff": 0.55, "deepseek": 0.95, "qwen": 0.7, "gpt": 0.6,
}

CRYPTO_KEYWORDS: dict[str, float] = {
    "bitcoin": 1.0, "btc": 0.9, "ethereum": 0.95, "eth": 0.75, "solana": 0.9, "xrp": 0.8,
    "ripple": 0.75, "cardano": 0.7, "dogecoin": 0.8, "polkadot": 0.65, "avalanche": 0.7,
    "litecoin": 0.65, "chainlink": 0.7, "monero": 0.6, "tron": 0.6, "polygon": 0.65,
    "sec": 0.9, "cftc": 0.8, "gensler": 0.8, "regulation": 0.75, "regulatory": 0.7,
    "bill": 0.6, "act": 0.5, "lawsuit": 0.75, "settlement": 0.6, "ban": 0.65,
    "etf": 0.95, "spot etf": 1.0, "blackrock": 0.8, "fidelity": 0.7, "grayscale": 0.7,
    "microstrategy": 0.75, "strategy": 0.5, "treasury": 0.55, "institutional": 0.65,
    "halving": 0.85, "block reward": 0.7, "staking": 0.65, "yield": 0.45, "airdrop": 0.7,
    "stablecoin": 0.9, "usdt": 0.7, "usdc": 0.7, "tether": 0.7, "circle": 0.6,
    "defi": 0.75, "nft": 0.55, "dao": 0.6, "depin": 0.6, "rwa": 0.7,
    "hack": 0.95, "hacked": 0.9, "exploit": 0.95, "breach": 0.9, "rug pull": 0.9,
    "scam": 0.8, "phishing": 0.7, "theft": 0.8, "stolen": 0.8, "seized": 0.75,
    "crash": 0.85, "surge": 0.7, "plunge": 0.8, "rally": 0.7, "record high": 0.8,
    "all-time high": 0.85, "ath": 0.7, "liquidation": 0.7, "mining": 0.55,
    "web3": 0.6, "exchange": 0.6, "binance": 0.85, "coinbase": 0.85, "kraken": 0.7,
    "custody": 0.5, "wallet": 0.5, "tokenization": 0.65, "prediction market": 0.6,
}

KEYWORDS: dict[Category, dict[str, float]] = {
    Category.AI: AI_KEYWORDS,
    Category.CRYPTO: CRYPTO_KEYWORDS,
}

#: Trọng số các thành phần (tổng = 1.0)
WEIGHTS: dict[str, float] = {
    "freshness": 0.35,
    "keyword": 0.30,
    "source_count": 0.20,
    "topic_volume": 0.15,
}

#: Ngưỡng "số bài coi như chủ đề nóng" để chuẩn hóa topic_volume.
TOPIC_VOLUME_SATURATION = 8.0
TOPIC_TITLE_OVERLAP = 0.4

_WORD_RE = re.compile(r"[a-z0-9$]+(?:[.\-][a-z0-9]+)*")


def keyword_score(text: str, category: Category) -> float:
    """Điểm từ khóa: lấy trọng số lớn nhất trong bài, có cộng dồn nhẹ cho nhiều từ khóa.

    Bài nói về nhiều chủ đề nóng vẫn nổi bật, nhưng không bị cộng dồn vô hạn.
    """
    normalized = normalize_title(text)
    tokens = set(_WORD_RE.findall(normalized))
    table = KEYWORDS.get(category, {})
    hits: list[tuple[str, float]] = []
    for keyword, weight in table.items():
        normalized_keyword = normalize_title(keyword)
        parts = normalized_keyword.split()
        if len(parts) == 1:
            if parts[0] in tokens:
                hits.append((keyword, weight))
        elif f" {normalized_keyword} " in f" {normalized} ":
            hits.append((keyword, weight))
    if not hits:
        return 0.0
    best = max(weight for _, weight in hits)
    bonus = 0.05 * (len(hits) - 1)
    return min(1.0, best + bonus)


def freshness_score(
    published_at: datetime | None,
    now: datetime,
    half_life_hours: float,
) -> float:
    """Điểm độ mới: giảm dần theo hàm mũ, nửa đời mặc định 6 giờ."""
    age = hours_since(published_at, now)
    if math.isinf(age):
        # Không có mốc thời gian: coi như vừa đăng nhưng không được hưởng điểm tối đa
        return 0.5
    return float(0.5 ** (age / half_life_hours))


def source_count_score(source_count: int) -> float:
    """Điểm đa nguồn: 1 nguồn = 0, từ 4 nguồn trở lên = 1."""
    if source_count <= 1:
        return 0.0
    return min(1.0, (source_count - 1) / 3.0)


def topic_volume_score(count: int) -> float:
    """Điểm độ phổ biến chủ đề dựa trên số bài cùng chủ đề trong cửa sổ thời gian."""
    if count <= 1:
        return 0.0
    return min(1.0, (count - 1) / (TOPIC_VOLUME_SATURATION - 1))


def combine(
    freshness: float,
    keywords: float,
    sources: float,
    volume: float,
    engagement: float = 0.0,
) -> float:
    """Gộp các thành phần thành điểm cuối cùng trong khoảng [0, 1]."""
    total = (
        WEIGHTS["freshness"] * freshness
        + WEIGHTS["keyword"] * keywords
        + WEIGHTS["source_count"] * sources
        + WEIGHTS["topic_volume"] * volume
    )
    if engagement > 0:
        # V1 chưa lấy engagement; khi có, giảm bớt phần còn lại cho tổng.
        reserved = 1.0 - sum(WEIGHTS.values())
        total = total * (1.0 - reserved) + reserved * min(1.0, engagement)
    return max(0.0, min(1.0, total))


class TrendAnalyzer:
    """Tính và cập nhật trend_score."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ------------------------------------------------------------------

    def score_item(
        self,
        item: NewsItem,
        now: datetime,
        topic_count: int = 1,
        source_count: int = 1,
    ) -> TrendComponents:
        """Tính điểm cho một bài vừa crawl."""
        freshness = freshness_score(
            item.published_at, now, self._settings.trend_freshness_half_life_hours
        )
        keywords = keyword_score(item.text_for_trend, item.category)
        sources = source_count_score(source_count)
        volume = topic_volume_score(topic_count)
        return TrendComponents(
            freshness=freshness,
            keyword=keywords,
            source_count=sources,
            topic_volume=volume,
            engagement=0.0,
            total=combine(freshness, keywords, sources, volume),
        )

    def score_batch(self, items: list[NewsItem], now: datetime | None = None) -> list[TrendComponents]:
        """Tính điểm cho cả lô, có tính đến mật độ bài cùng chủ đề."""
        reference = now or utcnow()
        window = timedelta(minutes=self._settings.trend_topic_window_minutes)
        recent_cutoff = reference - window

        recent = [
            item
            for item in items
            if item.published_at is None or item.published_at >= recent_cutoff
        ]
        scores: list[TrendComponents] = []
        for item in items:
            tokens = token_set(item.title)
            topic_count = sum(
                1
                for other in recent
                if other.category is item.category
                and (
                    other is item
                    or overlap_coefficient(tokens, token_set(other.title)) >= TOPIC_TITLE_OVERLAP
                )
            )
            scores.append(
                self.score_item(
                    item,
                    reference,
                    topic_count=max(1, topic_count),
                    source_count=1,
                )
            )
        return scores

    def score_record(
        self,
        record: NewsRecord,
        now: datetime | None = None,
        topic_count: int = 1,
    ) -> TrendComponents:
        """Tính lại điểm cho một bản tin đã lưu (đã biết source_count)."""
        reference = now or utcnow()
        freshness = freshness_score(
            record.published_at or record.first_seen_at,
            reference,
            self._settings.trend_freshness_half_life_hours,
        )
        keywords = keyword_score(f"{record.title} {record.summary or ''}", record.category)
        sources = source_count_score(record.source_count)
        volume = topic_volume_score(topic_count)
        return TrendComponents(
            freshness=freshness,
            keyword=keywords,
            source_count=sources,
            topic_volume=volume,
            engagement=0.0,
            total=combine(freshness, keywords, sources, volume),
        )

    def is_trending(self, score: float) -> bool:
        return score >= self._settings.trend_min_score


__all__ = [
    "AI_KEYWORDS",
    "CRYPTO_KEYWORDS",
    "KEYWORDS",
    "WEIGHTS",
    "TrendAnalyzer",
    "combine",
    "freshness_score",
    "keyword_score",
    "source_count_score",
    "topic_volume_score",
]
