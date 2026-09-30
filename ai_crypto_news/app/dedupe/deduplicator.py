"""Chống trùng tin theo tầng: canonical URL → content hash → fuzzy (cùng nguồn / khác nguồn)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from app.config import Settings
from app.database.models import NewsItem
from app.database.repository import Fingerprint, NewsRepository
from app.dedupe.normalizer import jaccard, overlap_coefficient
from app.logging_config import get_logger

logger = get_logger(__name__)


class MatchKind(StrEnum):
    """Cách khớp của một bản tin mới so với dữ liệu đã có."""

    NEW = "new"
    URL = "url"
    HASH = "hash"
    FUZZY = "fuzzy"


@dataclass(slots=True, frozen=True)
class DedupDecision:
    """Kết quả phân loại một bản tin."""

    item: NewsItem
    kind: MatchKind
    news_id: int | None = None
    similarity: float = 0.0

    @property
    def is_duplicate(self) -> bool:
        return self.kind is not MatchKind.NEW


class Deduplicator:
    """Quyết định một bản tin là mới hay đã tồn tại.

    Index được nạp một lần từ database cho mỗi vòng crawl nên không có
    truy vấn DB nào chạy cho từng bài trong phần lớn trường hợp.
    """

    def __init__(self, repository: NewsRepository, settings: Settings) -> None:
        self._repo = repository
        self._settings = settings
        self._by_url: dict[str, int] = {}
        self._by_hash: dict[str, int] = {}
        self._fingerprints: list[Fingerprint] = []
        self._token_index: dict[str, list[int]] = {}

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    async def load_index(self, now: datetime | None = None) -> int:
        """Nạp dấu vân tay từ database, sẵn sàng cho vòng crawl mới."""
        fingerprints = await self._repo.load_fingerprints(
            window_days=self._settings.dedupe_window_days,
            now=now,
        )
        self._by_url.clear()
        self._by_hash.clear()
        self._token_index.clear()
        self._fingerprints = fingerprints
        for position, fingerprint in enumerate(fingerprints):
            if fingerprint.canonical_url:
                self._by_url.setdefault(fingerprint.canonical_url, fingerprint.news_id)
            if fingerprint.content_hash:
                self._by_hash.setdefault(fingerprint.content_hash, fingerprint.news_id)
            self._index_tokens(fingerprint.title_tokens, position)
        logger.info(
            "Đã nạp index dedup: %d bản tin trong %d ngày",
            len(fingerprints),
            self._settings.dedupe_window_days,
        )
        return len(fingerprints)

    def _index_tokens(self, tokens: frozenset[str], position: int) -> None:
        for token in tokens:
            self._token_index.setdefault(token, []).append(position)

    def _register(self, news_id: int, item: NewsItem) -> None:
        """Đưa một bản tin mới vào index để các bài sau trong cùng lô khớp với nó."""
        if item.canonical_url:
            self._by_url.setdefault(item.canonical_url, news_id)
        if item.content_hash:
            self._by_hash.setdefault(item.content_hash, news_id)
        self._fingerprints.append(
            Fingerprint(
                news_id=news_id,
                content_hash=item.content_hash,
                canonical_url=item.canonical_url,
                simhash=item.simhash,
                title_tokens=item.title_tokens,
                published_at=item.published_at,
                source_count=1,
                is_reported=False,
                title=item.title,
                source=item.source,
                category=item.category.value,
            )
        )
        self._index_tokens(item.title_tokens, len(self._fingerprints) - 1)

    # ------------------------------------------------------------------
    # Phân loại
    # ------------------------------------------------------------------

    async def classify(self, item: NewsItem) -> DedupDecision:
        """Phân loại một bản tin (giả định đã gọi :meth:`enrich`)."""
        if not item.canonical_url or not item.content_hash or not item.title_tokens:
            item.enrich()

        # Tầng 1: URL chuẩn hóa trùng khớp
        matched_id = self._by_url.get(item.canonical_url)
        if matched_id is not None:
            return DedupDecision(item, MatchKind.URL, matched_id, 1.0)

        # Tầng 2: content hash (tiêu đề chuẩn hóa + nguồn) trùng khớp
        matched_id = self._by_hash.get(item.content_hash)
        if matched_id is not None:
            return DedupDecision(item, MatchKind.HASH, matched_id, 1.0)

        # Tầng 2b: index trong RAM có thể bỏ sót tin cũ ngoài cửa sổ → hỏi DB
        db_id = await self._repo.find_id_by_canonical_url(item.canonical_url)
        if db_id is None:
            db_id = await self._repo.find_id_by_content_hash(item.content_hash)
        if db_id is not None:
            return DedupDecision(item, MatchKind.HASH, db_id, 1.0)

        # Tầng 3: fuzzy — simhash gần nhau + tập token tiêu đề gần giống
        best = self._best_fuzzy_match(item)
        if best is not None:
            fingerprint, similarity = best
            return DedupDecision(item, MatchKind.FUZZY, fingerprint.news_id, similarity)

        return DedupDecision(item, MatchKind.NEW, None)

    def _best_fuzzy_match(self, item: NewsItem) -> tuple[Fingerprint, float] | None:
        """Tìm bản tin đã có gần giống nhất.

        Hai ngưỡng tách biệt, đo được trên dữ liệu thật:

        * **Cùng nguồn** — tiêu đề chỉ bị sửa/sắp lại, dùng Jaccard nghiêm ngặt.
        * **Khác nguồn** — cùng một sự kiện nhưng tiêu đề được viết lại, dùng
          hệ số chồng lấn (nhẹ hơn Jaccard vì tiêu đề ngắn) kèm giới hạn
          khoảng cách thời gian đăng. Ngưỡng mặc định rất cao (0.80) vì không
          có cách phân biệt chắc chắn "cùng sự kiện" với "cùng chủ đề, khác
          sự kiện"; đặt ``dedupe_cross_source_overlap = 0.0`` để tắt hẳn.
        """
        if not item.title_tokens:
            return None
        min_jaccard = self._settings.dedupe_jaccard_threshold
        min_cross_overlap = self._settings.dedupe_cross_source_overlap
        if min_jaccard <= 0 and min_cross_overlap <= 0:
            return None

        best: tuple[Fingerprint, float] | None = None
        for position in self._candidate_positions(item.title_tokens):
            fingerprint = self._fingerprints[position]
            if not fingerprint.title_tokens:
                continue
            if fingerprint.category != item.category.value:
                continue

            same_source = fingerprint.source == item.source
            if same_source:
                if min_jaccard <= 0:
                    continue
                score = jaccard(item.title_tokens, fingerprint.title_tokens)
                if score < min_jaccard:
                    continue
            else:
                if min_cross_overlap <= 0:
                    continue
                if not self._within_cross_source_window(item, fingerprint):
                    continue
                score = overlap_coefficient(item.title_tokens, fingerprint.title_tokens)
                if score < min_cross_overlap:
                    continue

            if best is None or score > best[1]:
                best = (fingerprint, score)
        return best

    def _candidate_positions(self, tokens: frozenset[str]) -> set[int]:
        """Ứng viên có chung ít nhất một token tiêu đề với bài đang xét.

        Rẻ hơn nhiều so với quét toàn bộ index, và hợp lý về mặt ngữ nghĩa:
        hai bản tin trùng nhau gần như luôn có ít nhất một từ khóa chung.
        """
        positions: set[int] = set()
        for token in tokens:
            positions.update(self._token_index.get(token, ()))
        return positions

    def _within_cross_source_window(self, item: NewsItem, fingerprint: Fingerprint) -> bool:
        """Hai tin khác nguồn chỉ được gom nếu thời gian đăng gần nhau."""
        if fingerprint.published_at is None or item.published_at is None:
            return True
        delta = abs((item.published_at - fingerprint.published_at).total_seconds())
        limit = self._settings.dedupe_cross_source_window_hours * 3600
        return delta <= limit

    async def classify_many(self, items: list[NewsItem]) -> list[DedupDecision]:
        """Phân loại cả lô bài, tự cập nhật index sau từng kết quả.

        Nhờ vậy cùng một tin xuất hiện ở 3 nguồn trong cùng một vòng crawl
        chỉ tạo ra 1 bản tin mới, 2 tin còn lại được ghi vào ``news_sources``.

        Với các bài trùng nhau *trong cùng lô*, ``news_id`` được gán tạm
        bằng số âm (``-(thứ tự + 1)``); gọi :meth:`resolve_ids` sau khi
        insert database để thay bằng id thật.
        """
        decisions: list[DedupDecision] = []
        new_count = 0

        for item in items:
            item.enrich()
            decision = await self.classify(item)
            if decision.is_duplicate:
                decisions.append(decision)
                continue
            new_count += 1
            self._register(-new_count, item)
            # Bài mới cũng mang id tạm để resolve_ids() thay bằng id thật sau khi insert.
            decisions.append(DedupDecision(item, MatchKind.NEW, -new_count, 0.0))

        logger.info(
            "Dedup trong lô: %d bài, %d mới, %d trùng",
            len(items),
            new_count,
            len(items) - new_count,
        )
        return decisions

    def resolve_ids(
        self,
        decisions: list[DedupDecision],
        inserted: list[tuple[int, NewsItem]],
    ) -> list[DedupDecision]:
        """Thay ``news_id`` tạm (số âm) bằng id thật trong database.

        ``inserted`` là kết quả ``repository.insert_items()``: cặp ``(id, item)``.
        """
        if not inserted:
            return decisions
        temporary_ids = {
            id(decision.item): decision.news_id
            for decision in decisions
            if decision.kind is MatchKind.NEW
            and decision.news_id is not None
            and decision.news_id < 0
        }
        replacements: dict[int, int] = {}
        for news_id, item in inserted:
            temporary_id = temporary_ids.get(id(item))
            if temporary_id is not None:
                replacements[temporary_id] = news_id
        resolved: list[DedupDecision] = []
        for decision in decisions:
            news_id = decision.news_id
            if news_id is not None and news_id in replacements:
                resolved.append(
                    DedupDecision(decision.item, decision.kind, replacements[news_id], decision.similarity)
                )
            else:
                resolved.append(decision)

        # Cập nhật lại index trong RAM
        self._fingerprints = [
            replace(fingerprint, news_id=replacements.get(fingerprint.news_id, fingerprint.news_id))
            for fingerprint in self._fingerprints
        ]
        for url, old_id in list(self._by_url.items()):
            if old_id in replacements:
                self._by_url[url] = replacements[old_id]
        for content_hash, old_id in list(self._by_hash.items()):
            if old_id in replacements:
                self._by_hash[content_hash] = replacements[old_id]
        return resolved
