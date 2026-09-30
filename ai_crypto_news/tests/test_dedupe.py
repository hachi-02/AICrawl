"""Test lớp chống trùng tin: URL, hash, fuzzy cùng nguồn, fuzzy khác nguồn, lô tin."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import Settings
from app.database.models import Category, NewsItem
from app.database.repository import NewsRepository
from app.dedupe.deduplicator import Deduplicator, MatchKind
from tests.conftest import NOW, make_item


async def _seed(repo: NewsRepository, dedup: Deduplicator, items: list[NewsItem]) -> list[int]:
    """Đưa các bài mẫu vào DB rồi nạp lại index, trả về id theo thứ tự."""
    inserted = await repo.insert_items(items, now=NOW)
    await dedup.load_index(now=NOW)
    return [news_id for news_id, _ in inserted]


class TestExactLayers:
    async def test_canonical_url_match(self, repository: NewsRepository, settings: Settings) -> None:
        dedup = Deduplicator(repository, settings)
        await _seed(repository, dedup, [make_item("OpenAI ships GPT-5", url="https://techcrunch.com/a?utm_source=x")])

        # URL khác hoàn toàn nhưng cùng canonical -> trùng
        decision = await dedup.classify(
            make_item("Bài hoàn toàn khác tiêu đề", url="https://techcrunch.com/a/")
        )
        assert decision.kind is MatchKind.URL
        assert decision.news_id == 1
        assert decision.is_duplicate

    async def test_content_hash_match_same_source(self, repository: NewsRepository, settings: Settings) -> None:
        dedup = Deduplicator(repository, settings)
        await _seed(repository, dedup, [make_item("OpenAI ships GPT-5 to everyone")])

        # URL khác nhưng tiêu đề giống hệt cùng nguồn -> trùng
        decision = await dedup.classify(
            make_item("OpenAI ships GPT-5 to everyone!", url="https://techcrunch.com/2026/09/29/ship-it/")
        )
        assert decision.kind is MatchKind.HASH
        assert decision.news_id == 1

    async def test_same_title_different_source_is_not_exact(self, repository: NewsRepository, settings: Settings) -> None:
        dedup = Deduplicator(repository, settings)
        await _seed(repository, dedup, [make_item("Bitcoin ETF approved", source="coindesk")])

        decision = await dedup.classify(
            make_item("Bitcoin ETF approved", source="decrypt", url="https://decrypt.co/news/btc")
        )
        # content_hash có chứa nguồn nên không trùng chính xác;
        # chỉ lớp cross-source mới có thể gom, mặc định rất thận trọng.
        assert decision.kind in (MatchKind.FUZZY, MatchKind.NEW)

    async def test_old_item_outside_window_found_via_db(
        self, repository: NewsRepository, settings: Settings
    ) -> None:
        dedup = Deduplicator(repository, settings)
        old = make_item("Bài cũ ngoài cửa sổ dedup", published_at=NOW - timedelta(days=30))
        inserted = await repository.insert_items([old], now=NOW - timedelta(days=30))
        await dedup.load_index(now=NOW)
        assert not any(f.news_id == inserted[0][0] for f in dedup._fingerprints)

        # index RAM không có nhưng DB vẫn phải bắt được
        decision = await dedup.classify(
            make_item("Bài cũ ngoài cửa sổ dedup", published_at=NOW - timedelta(days=30))
        )
        assert decision.kind is MatchKind.HASH
        assert decision.news_id == inserted[0][0]


class TestFuzzySameSource:
    async def test_minor_rewrite_merged(self, repository: NewsRepository, settings: Settings) -> None:
        dedup = Deduplicator(repository, settings)
        await _seed(repository, dedup, [make_item("OpenAI releases GPT-5 model to all users")])

        decision = await dedup.classify(
            make_item("OpenAI releases GPT-5 model for free to users", url="https://techcrunch.com/other/")
        )
        assert decision.kind is MatchKind.FUZZY
        assert decision.news_id == 1
        assert decision.similarity >= settings.dedupe_jaccard_threshold

    async def test_different_story_not_merged(self, repository: NewsRepository, settings: Settings) -> None:
        dedup = Deduplicator(repository, settings)
        await _seed(repository, dedup, [make_item("OpenAI releases GPT-5 model to all users")])

        decision = await dedup.classify(
            make_item("Nvidia unveils new AI accelerator chip", url="https://techcrunch.com/nvidia/")
        )
        assert decision.kind is MatchKind.NEW
        assert decision.news_id is None

    async def test_strict_threshold_blocks_weak_match(
        self, repository: NewsRepository, tmp_path
    ) -> None:
        strict = Settings(
            _env_file=None,
            database_path=tmp_path / "s.db",
            dedupe_jaccard_threshold=0.99,
            dedupe_cross_source_overlap=0.0,
        )
        dedup = Deduplicator(repository, strict)
        await _seed(repository, dedup, [make_item("OpenAI releases GPT-5 model to all users")])

        decision = await dedup.classify(
            make_item("OpenAI releases GPT-5 model for free to users", url="https://techcrunch.com/other/")
        )
        assert decision.kind is MatchKind.NEW


class TestFuzzyCrossSource:
    async def test_disabled_by_default_when_threshold_zero(
        self, repository: NewsRepository, tmp_path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "s.db",
            dedupe_jaccard_threshold=0.82,
            dedupe_cross_source_overlap=0.0,
        )
        dedup = Deduplicator(repository, settings)
        await _seed(
            repository,
            dedup,
            [make_item("BlackRock spot Bitcoin ETF gets SEC green light after years", source="coindesk")],
        )

        decision = await dedup.classify(
            make_item(
                "BlackRock spot Bitcoin ETF gets SEC green light after years",
                source="cointelegraph",
                url="https://cointelegraph.com/news/btc-etf",
            )
        )
        assert decision.kind is MatchKind.NEW

    async def test_enabled_merges_rewritten_headline(
        self, repository: NewsRepository, tmp_path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "s.db",
            dedupe_cross_source_overlap=0.7,
            dedupe_cross_source_window_hours=36,
        )
        dedup = Deduplicator(repository, settings)
        await _seed(
            repository,
            dedup,
            [make_item("Bitcoin spot ETF approved by SEC after years of delays", source="coindesk")],
        )

        decision = await dedup.classify(
            make_item(
                "SEC approves spot bitcoin ETF, ending years of delay",
                source="cointelegraph",
                url="https://cointelegraph.com/news/sec-etf",
            )
        )
        assert decision.kind is MatchKind.FUZZY
        assert decision.news_id == 1

    async def test_outside_time_window_not_merged(
        self, repository: NewsRepository, tmp_path
    ) -> None:
        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "s.db",
            dedupe_cross_source_overlap=0.7,
            dedupe_cross_source_window_hours=12,
        )
        dedup = Deduplicator(repository, settings)
        await _seed(
            repository,
            dedup,
            [
                make_item(
                    "Bitcoin spot ETF approved by SEC after years of delays",
                    source="coindesk",
                    published_at=NOW - timedelta(days=5),
                )
            ],
        )

        decision = await dedup.classify(
            make_item(
                "SEC approves spot bitcoin ETF, ending years of delay",
                source="cointelegraph",
                url="https://cointelegraph.com/news/sec-etf",
                published_at=NOW,
            )
        )
        assert decision.kind is MatchKind.NEW

    async def test_unrelated_article_never_merged(
        self, repository: NewsRepository, tmp_path
    ) -> None:
        settings = Settings(
            _env_file=None, database_path=tmp_path / "s.db", dedupe_cross_source_overlap=0.5
        )
        dedup = Deduplicator(repository, settings)
        await _seed(
            repository,
            dedup,
            [make_item("Nvidia beats earnings estimates again", source="coindesk", category=Category.CRYPTO)],
        )
        decision = await dedup.classify(
            make_item(
                "Apple delays foldable phone launch to 2027",
                source="theverge",
                url="https://theverge.com/apple",
                category=Category.AI,
            )
        )
        assert decision.kind is MatchKind.NEW


class TestBatch:
    async def test_same_story_three_sources_one_record(
        self, repository: NewsRepository, settings: Settings
    ) -> None:
        dedup = Deduplicator(repository, settings)
        items = [
            make_item("OpenAI releases GPT-5 model to all users", source="techcrunch", url="https://techcrunch.com/1/"),
            make_item("OpenAI releases GPT-5 model to all users!", source="venturebeat", url="https://venturebeat.com/1/"),
            make_item(
                "OpenAI releases GPT-5 model for free to users", source="theverge", url="https://theverge.com/1/"
            ),
        ]
        decisions = await dedup.classify_many(items)
        new = [d for d in decisions if not d.is_duplicate]
        assert len(new) == 1
        assert new[0].news_id == -1  # id tạm
        assert all(d.news_id == -1 for d in decisions if d.is_duplicate)

    async def test_resolve_ids_replaces_negative_ids(
        self, repository: NewsRepository, settings: Settings
    ) -> None:
        dedup = Deduplicator(repository, settings)
        items = [
            make_item("OpenAI releases GPT-5 model to all users", source="techcrunch", url="https://techcrunch.com/1/"),
            make_item("OpenAI releases GPT-5 model to all users!", source="venturebeat", url="https://venturebeat.com/1/"),
        ]
        decisions = await dedup.classify_many(items)
        new_items = [d.item for d in decisions if not d.is_duplicate]
        inserted = await repository.insert_items(new_items, now=NOW)
        resolved = dedup.resolve_ids(decisions, inserted)

        assert {d.news_id for d in resolved} == {1}

    async def test_index_updated_after_batch(self, repository: NewsRepository, settings: Settings) -> None:
        dedup = Deduplicator(repository, settings)
        await dedup.load_index(now=NOW)
        await dedup.classify_many([make_item("Brand new headline about AI chips")])
        # bài vừa đăng ký phải khớp ngay trong lô kế tiếp
        decision = await dedup.classify(
            make_item("Brand new headline about AI chips", url="https://techcrunch.com/dup/")
        )
        assert decision.kind is MatchKind.HASH

    async def test_all_duplicates_resolve_is_noop(
        self, repository: NewsRepository, settings: Settings
    ) -> None:
        dedup = Deduplicator(repository, settings)
        decisions = await dedup.classify_many([make_item("Only one item here")])
        assert dedup.resolve_ids(decisions, []) == decisions


@pytest.mark.parametrize("engine_flag", ["dedupe_jaccard_threshold", "dedupe_cross_source_overlap"])
def test_fuzzy_fully_disabled_keeps_everything(
    repository: NewsRepository, tmp_path, engine_flag: str
) -> None:
    settings = Settings(_env_file=None, database_path=tmp_path / "s.db", **{engine_flag: 0.0})
    dedup = Deduplicator(repository, settings)
    assert dedup._best_fuzzy_match(make_item("Bất kỳ tiêu đề nào cũng mới")) is None
