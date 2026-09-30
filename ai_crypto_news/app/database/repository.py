"""Tất cả truy vấn SQL của project nằm ở đây.

Repository luôn dùng chung một kết nối của :class:`~app.database.database.Database`
nên không bao giờ tạo connection mới cho từng record.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.database.database import Database
from app.database.models import (
    Category,
    CrawlStats,
    NewsItem,
    NewsRecord,
    row_to_record,
)
from app.dedupe.normalizer import (
    canonicalize_url,
    parse_datetime,
    simhex,
    simint,
    to_iso,
    utcnow,
)
from app.logging_config import get_logger

logger = get_logger(__name__)

NEWS_COLUMNS = """
    n.id, n.title, n.url, n.source, n.category, n.author, n.published_at,
    n.summary, n.image_url, n.content_hash, n.canonical_url, n.simhash,
    n.source_count, n.trend_score, n.is_reported, n.reported_at,
    n.telegram_message_id, n.first_seen_at, n.crawled_at
"""


@dataclass(slots=True, frozen=True)
class Fingerprint:
    """Dấu vân tay của một bản tin đã lưu, dùng để so khớp fuzzy."""

    news_id: int
    content_hash: str
    canonical_url: str
    simhash: int
    title_tokens: frozenset[str]
    published_at: datetime | None
    source_count: int
    is_reported: bool
    title: str
    source: str = ""
    category: str = ""


class NewsRepository:
    """Lớp truy cập dữ liệu cho bảng ``news`` và ``news_sources``."""

    def __init__(self, database: Database) -> None:
        self.db = database

    # ------------------------------------------------------------------
    # Khởi tạo
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        await self.db.initialize()

    async def close(self) -> None:
        await self.db.close()

    # ------------------------------------------------------------------
    # Tra cứu nhanh (exact match)
    # ------------------------------------------------------------------

    async def find_id_by_url(self, url: str) -> int | None:
        row = await self.db.fetch_one("SELECT id FROM news WHERE url = ? OR canonical_url = ?", (url, url))
        return int(row["id"]) if row else None

    async def find_id_by_canonical_url(self, canonical_url: str) -> int | None:
        if not canonical_url:
            return None
        row = await self.db.fetch_one("SELECT id FROM news WHERE canonical_url = ?", (canonical_url,))
        return int(row["id"]) if row else None

    async def find_id_by_content_hash(self, content_hash: str) -> int | None:
        if not content_hash:
            return None
        row = await self.db.fetch_one("SELECT id FROM news WHERE content_hash = ?", (content_hash,))
        return int(row["id"]) if row else None

    async def load_fingerprints(self, window_days: int, now: datetime | None = None) -> list[Fingerprint]:
        """Nạp dấu vân tay các bản tin trong cửa sổ thời gian gần đây.

        Dùng cho fuzzy matching; nạp một lần mỗi vòng crawl để không phải
        query DB cho từng bài.
        """
        reference = now or utcnow()
        since = to_iso(reference - timedelta(days=window_days))
        rows = await self.db.fetch_all(
            """
            SELECT id, content_hash, canonical_url, simhash, title_tokens,
                   published_at, source_count, is_reported, title, source, category
            FROM news
            WHERE first_seen_at >= ?
            """,
            (since,),
        )
        fingerprints: list[Fingerprint] = []
        for row in rows:
            tokens = frozenset((row["title_tokens"] or "").split())
            fingerprints.append(
                Fingerprint(
                    news_id=int(row["id"]),
                    content_hash=row["content_hash"] or "",
                    canonical_url=row["canonical_url"] or "",
                    simhash=simint(row["simhash"]),
                    title_tokens=tokens,
                    published_at=parse_datetime(row["published_at"]),
                    source_count=int(row["source_count"] or 1),
                    is_reported=bool(row["is_reported"]),
                    title=row["title"] or "",
                    source=row["source"] or "",
                    category=row["category"] or "",
                )
            )
        return fingerprints

    # ------------------------------------------------------------------
    # Ghi dữ liệu
    # ------------------------------------------------------------------

    async def insert_items(
        self,
        items: Sequence[NewsItem],
        now: datetime | None = None,
    ) -> list[tuple[int, NewsItem]]:
        """Insert nhiều bản tin mới trong MỘT transaction.

        Trả về danh sách ``(id, item)`` cho đúng những bản tin thực sự được
        tạo, nhờ đó thứ tự id luôn khớp với thứ tự đầu vào.
        """
        if not items:
            return []
        timestamp = to_iso(now or utcnow())
        sql = """
            INSERT OR IGNORE INTO news (
                title, url, canonical_url, source, category, author, published_at,
                summary, image_url, content_hash, simhash, title_tokens,
                source_count, trend_score, is_reported, first_seen_at, crawled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0.0, 0, ?, ?)
        """
        created: list[tuple[int, NewsItem]] = []
        async with self.db.transaction() as connection:
            cursor = await connection.cursor()
            try:
                for item in items:
                    await cursor.execute(
                        sql,
                        (
                            item.title,
                            item.url,
                            item.canonical_url or item.url,
                            item.source,
                            item.category.value,
                            item.author,
                            to_iso(item.published_at),
                            item.summary,
                            item.image_url,
                            item.content_hash,
                            simhex(item.simhash),
                            " ".join(sorted(item.title_tokens)),
                            timestamp,
                            timestamp,
                        ),
                    )
                    if cursor.rowcount:
                        created.append((int(cursor.lastrowid or 0), item))
            finally:
                await cursor.close()
        if len(created) != len(items):
            logger.warning(
                "Chỉ %d/%d bản tin được tạo (phần còn lại trùng hash ở tầng UNIQUE)",
                len(created), len(items),
            )
        else:
            logger.debug("Đã insert %d bản tin mới", len(created))
        return created

    async def link_source(self, news_id: int, item: NewsItem, now: datetime | None = None) -> bool:
        """Gắn thêm nguồn/URL thứ 2 trở đi cho một bản tin đã tồn tại.

        Trả về True nếa gắn mới. Các trường hợp trả về False:

        * bản tin gốc không tồn tại;
        * URL đã có trong ``news_sources``;
        * URL trùng chính bản tin gốc (cùng một bài bị lấy ra ở 2 mục của
          cùng nguồn) — không phải nguồn mới nên không tăng ``source_count``.

        ``source_count`` luôn là **số nguồn phân biệt**: nếu cùng một nguồn phụ
        xuất hiện ở nhiều URL khác nhau thì chỉ được tính một lần.
        """
        timestamp = to_iso(now or utcnow())
        async with self.db.transaction() as connection:
            cursor = await connection.cursor()
            try:
                await cursor.execute(
                    "SELECT source, url, canonical_url FROM news WHERE id = ?", (news_id,)
                )
                row = await cursor.fetchone()
                if row is None:
                    logger.warning("link_source: không tìm thấy news id=%s", news_id)
                    return False
                primary_source, primary_url, canonical = row[0], row[1], row[2] or ""
                # So sánh trên dạng canonical để bắt biến thể trailing slash / query.
                item_canonical = canonicalize_url(item.url)
                if item.url in (primary_url, canonical) or (
                    item_canonical and item_canonical in (primary_url, canonical)
                ):
                    return False
                # `source_count` đếm số nguồn phân biệt, nên phải hỏi trước khi
                # insert: cùng một nguồn phụ có nhiều URL chỉ được tính một lần.
                is_new_source = item.source != primary_source
                if is_new_source:
                    await cursor.execute(
                        "SELECT 1 FROM news_sources WHERE news_id = ? AND source = ? LIMIT 1",
                        (news_id, item.source),
                    )
                    is_new_source = await cursor.fetchone() is None
                await cursor.execute(
                    """
                    INSERT OR IGNORE INTO news_sources (news_id, source, url, author, published_at, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (news_id, item.source, item.url, item.author, to_iso(item.published_at), timestamp),
                )
                if not cursor.rowcount:
                    return False
                if is_new_source:
                    await cursor.execute(
                        "UPDATE news SET source_count = source_count + 1 WHERE id = ?",
                        (news_id,),
                    )
                # Nguồn thứ 2 có thể đăng sớm hơn: giữ mốc sớm nhất để tính trend đúng.
                await cursor.execute(
                    """
                    UPDATE news SET published_at = ?
                    WHERE id = ? AND published_at IS NOT NULL AND ? IS NOT NULL AND ? < published_at
                    """,
                    (to_iso(item.published_at), news_id, to_iso(item.published_at), to_iso(item.published_at)),
                )
            finally:
                await cursor.close()
        return True

    async def get_source_counts(self, news_ids: Sequence[int]) -> dict[int, int]:
        """Lấy ``source_count`` hiện tại của nhiều bản tin (dùng khi tính trend).

        Cần đọc lại sau khi đã gắn nguồn, vì tin mới có thể được link thêm nguồn
        phụ ngay trong cùng vòng ghi.
        """
        ids = [int(news_id) for news_id in news_ids]
        if not ids:
            return {}
        placeholders = ", ".join("?" * len(ids))
        async with self.db.transaction() as connection:
            cursor = await connection.cursor()
            try:
                await cursor.execute(
                    f"SELECT id, source_count FROM news WHERE id IN ({placeholders})",  # noqa: S608
                    ids,
                )
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return {int(row[0]): int(row[1] or 1) for row in rows}

    async def update_trend_scores(self, scores: dict[int, float]) -> int:
        """Cập nhật trend_score cho nhiều bản tin trong một transaction."""
        if not scores:
            return 0
        payload = [(round(float(score), 6), news_id) for news_id, score in scores.items()]
        async with self.db.transaction() as connection:
            cursor = await connection.cursor()
            try:
                await cursor.executemany("UPDATE news SET trend_score = ? WHERE id = ?", payload)
            finally:
                await cursor.close()
        return len(payload)

    # ------------------------------------------------------------------
    # Đánh dấu đã gửi Telegram
    # ------------------------------------------------------------------

    async def is_reported(self, news_id: int) -> bool:
        value = await self.db.fetch_value(
            "SELECT is_reported FROM news WHERE id = ?",
            (news_id,),
            default=0,
        )
        return bool(value)

    async def mark_reported(
        self,
        news_id: int,
        message_id: int | None,
        chat_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Đánh dấu tin đã gửi. Trả về False nếu tin đã được đánh dấu trước đó.

        Việc đánh dấu và ghi log giao hàng nằm chung transaction để không
        xảy ra tình trạng gửi xong nhưng cờ chưa lên.
        """
        timestamp = to_iso(now or utcnow())
        async with self.db.transaction() as connection:
            cursor = await connection.cursor()
            try:
                await cursor.execute(
                    "SELECT is_reported FROM news WHERE id = ?",
                    (news_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    logger.error("mark_reported: không tìm thấy news id=%s", news_id)
                    return False
                if bool(row[0]):
                    return False
                await cursor.execute(
                    """
                    UPDATE news
                    SET is_reported = 1, reported_at = ?, telegram_message_id = ?
                    WHERE id = ?
                    """,
                    (timestamp, message_id, news_id),
                )
                await cursor.execute(
                    """
                    INSERT INTO telegram_deliveries (news_id, chat_id, message_id, sent_at, ok, error)
                    VALUES (?, ?, ?, ?, 1, NULL)
                    ON CONFLICT(news_id, chat_id) DO UPDATE SET
                        message_id = excluded.message_id,
                        sent_at = excluded.sent_at,
                        ok = 1,
                        error = NULL
                    """,
                    (news_id, chat_id, message_id, timestamp),
                )
            finally:
                await cursor.close()
        return True

    async def mark_report_failed(self, news_id: int, chat_id: str, error: str, now: datetime | None = None) -> None:
        """Ghi nhận lần gửi thất bại (không set is_reported để lần sau gửi lại)."""
        timestamp = to_iso(now or utcnow())
        await self.db.execute(
            """
            INSERT INTO telegram_deliveries (news_id, chat_id, message_id, sent_at, ok, error)
            VALUES (?, ?, NULL, ?, 0, ?)
            ON CONFLICT(news_id, chat_id) DO UPDATE SET
                sent_at = excluded.sent_at,
                ok = 0,
                error = excluded.error
            """,
            (news_id, chat_id, timestamp, error[:500]),
        )

    # ------------------------------------------------------------------
    # Truy vấn phục vụ báo cáo / lệnh bot
    # ------------------------------------------------------------------

    async def _rows_to_records(self, rows: Sequence[Any]) -> list[NewsRecord]:
        """Chuyển các dòng ``news`` thành record và gắn ``extra_urls`` từ ``news_sources``.

        Nạp nguồn phụ bằng **một** truy vấn cho cả lô để không tạy N+1 query.
        """
        if not rows:
            return []
        records = [row_to_record(row) for row in rows]
        by_id = {record.id: record for record in records}
        placeholders = ",".join("?" * len(by_id))
        extra_rows = await self.db.fetch_all(
            f"SELECT news_id, url FROM news_sources WHERE news_id IN ({placeholders}) ORDER BY id",
            tuple(by_id),
        )
        for row in extra_rows:
            record = by_id.get(int(row["news_id"]))
            if record is not None:
                record.extra_urls.append(row["url"])
        return records

    async def select_trending(
        self,
        category: Category,
        min_score: float,
        max_age_hours: float,
        limit: int,
        now: datetime | None = None,
        only_unreported: bool = True,
    ) -> list[NewsRecord]:
        """Lấy tin nổi bật theo category, mặc định chỉ lấy tin chưa gửi."""
        reference = now or utcnow()
        since = to_iso(reference - timedelta(hours=max_age_hours))
        where = [
            "n.category = ?",
            "n.trend_score >= ?",
            "COALESCE(n.published_at, n.first_seen_at) >= ?",
        ]
        params: list[object] = [category.value, float(min_score), since]
        if only_unreported:
            where.append("n.is_reported = 0")
        params.append(int(limit))
        rows = await self.db.fetch_all(
            f"""
            SELECT {NEWS_COLUMNS}
            FROM news n
            WHERE {' AND '.join(where)}
            ORDER BY n.trend_score DESC, COALESCE(n.published_at, n.first_seen_at) DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return await self._rows_to_records(rows)

    async def select_latest(
        self,
        limit: int = 10,
        category: Category | None = None,
        only_unreported: bool = False,
        now: datetime | None = None,
    ) -> list[NewsRecord]:
        """Lấy tin mới nhất, mới nhất lên đầu."""
        where: list[str] = []
        params: list[object] = []
        if category is not None:
            where.append("n.category = ?")
            params.append(category.value)
        if only_unreported:
            where.append("n.is_reported = 0")
        params.append(int(limit))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = await self.db.fetch_all(
            f"""
            SELECT {NEWS_COLUMNS}
            FROM news n
            {clause}
            ORDER BY COALESCE(n.published_at, n.first_seen_at) DESC, n.id DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return await self._rows_to_records(rows)

    async def select_unreported(
        self,
        min_score: float,
        max_age_hours: float,
        limit: int,
        now: datetime | None = None,
    ) -> list[NewsRecord]:
        """Lấy tin chưa từng gửi, đạt ngưỡng trend, bất kể category."""
        reference = now or utcnow()
        since = to_iso(reference - timedelta(hours=max_age_hours))
        rows = await self.db.fetch_all(
            f"""
            SELECT {NEWS_COLUMNS}
            FROM news n
            WHERE n.is_reported = 0
              AND n.trend_score >= ?
              AND COALESCE(n.published_at, n.first_seen_at) >= ?
            ORDER BY n.trend_score DESC, COALESCE(n.published_at, n.first_seen_at) DESC
            LIMIT ?
            """,
            (float(min_score), since, int(limit)),
        )
        return await self._rows_to_records(rows)

    async def get_record(self, news_id: int) -> NewsRecord | None:
        row = await self.db.fetch_one(
            f"SELECT {NEWS_COLUMNS} FROM news n WHERE n.id = ?",
            (news_id,),
        )
        return (await self._rows_to_records([row]))[0] if row else None

    async def list_sources_for(self, news_id: int) -> list[str]:
        rows = await self.db.fetch_all(
            "SELECT source, url FROM news_sources WHERE news_id = ? ORDER BY id",
            (news_id,),
        )
        return [f"{row['source']}: {row['url']}" for row in rows]

    # ------------------------------------------------------------------
    # Thống kê
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict[str, object]:
        """Thống kê tổng quan cho lệnh /status."""
        total = int(await self.db.fetch_value("SELECT COUNT(*) FROM news", default=0))
        reported = int(
            await self.db.fetch_value("SELECT COUNT(*) FROM news WHERE is_reported = 1", default=0)
        )
        by_category: dict[str, int] = {}
        for category in Category:
            by_category[category.value] = int(
                await self.db.fetch_value(
                    "SELECT COUNT(*) FROM news WHERE category = ?",
                    (category.value,),
                    default=0,
                )
            )
        last_24h = int(
            await self.db.fetch_value(
                "SELECT COUNT(*) FROM news WHERE first_seen_at >= ?",
                (to_iso(utcnow() - timedelta(hours=24)),),
                default=0,
            )
        )
        source_count = int(
            await self.db.fetch_value("SELECT COALESCE(SUM(source_count), 0) FROM news", default=0)
        )
        extra_sources = int(await self.db.fetch_value("SELECT COUNT(*) FROM news_sources", default=0))
        last_run = await self.db.fetch_one(
            """
            SELECT id, started_at, finished_at, articles_found, new_articles,
                   duplicates, telegram_sent, telegram_failed, sources_failed
            FROM crawl_runs ORDER BY id DESC LIMIT 1
            """
        )
        top_sources = await self.db.fetch_all(
            """
            SELECT source, COUNT(*) AS total
            FROM news GROUP BY source ORDER BY total DESC LIMIT 5
            """
        )
        return {
            "total": total,
            "reported": reported,
            "unreported": total - reported,
            "by_category": by_category,
            "last_24h": last_24h,
            "source_mentions": source_count,
            "extra_sources": extra_sources,
            "last_run": dict(last_run) if last_run else None,
            "top_sources": [(row["source"], int(row["total"])) for row in top_sources],
            "db_size_bytes": _file_size(self.db.path),
        }

    # ------------------------------------------------------------------
    # Vòng crawl
    # ------------------------------------------------------------------

    async def start_run(self, now: datetime | None = None) -> int:
        started_at = to_iso(now or utcnow())
        return await self.db.execute(
            "INSERT INTO crawl_runs (started_at, sources_total) VALUES (?, 0)",
            (started_at,),
        )

    async def finish_run(self, stats: CrawlStats) -> None:
        await self.db.execute(
            """
            UPDATE crawl_runs
            SET finished_at = ?, sources_total = ?, sources_ok = ?, sources_failed = ?,
                articles_found = ?, new_articles = ?, duplicates = ?,
                telegram_sent = ?, telegram_failed = ?, error_message = ?
            WHERE id = ?
            """,
            (
                to_iso(stats.finished_at or utcnow()),
                stats.sources_total,
                stats.sources_ok,
                stats.sources_failed,
                stats.articles_found,
                stats.new_articles,
                stats.duplicates,
                stats.telegram_sent,
                stats.telegram_failed,
                "; ".join(stats.errors)[:2000] or None,
                stats.run_id,
            ),
        )

    async def list_runs(self, limit: int = 5) -> list[dict[str, object]]:
        rows = await self.db.fetch_all(
            """
            SELECT id, started_at, finished_at, articles_found, new_articles,
                   duplicates, telegram_sent, telegram_failed, sources_failed
            FROM crawl_runs ORDER BY id DESC LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(row) for row in rows]


def _file_size(path: str) -> int:
    from pathlib import Path

    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


__all__ = ["NewsRepository", "Fingerprint", "UTC"]
