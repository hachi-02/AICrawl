"""Quản lý kết nối SQLite: khởi tạo schema, migration, WAL.

Chỉ tạo MỘT kết nối cho toàn process và dùng ``asyncio.Lock`` để truy cập tuần tự,
nhờ vậy không bao giờ mở connection mới cho từng record.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from app.logging_config import get_logger

logger = get_logger(__name__)

PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000",
    "PRAGMA temp_store=MEMORY",
)

#: Danh sách migration. Mỗi phần tử là (version, tên, nhiều câu SQL).
MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "init_schema",
        (
            """
            CREATE TABLE IF NOT EXISTS news (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                title               TEXT    NOT NULL,
                url                 TEXT    NOT NULL UNIQUE,
                canonical_url       TEXT    NOT NULL DEFAULT '',
                source              TEXT    NOT NULL,
                category            TEXT    NOT NULL CHECK (category IN ('AI', 'CRYPTO')),
                author              TEXT,
                published_at        TEXT,
                summary             TEXT,
                image_url           TEXT,
                content_hash        TEXT    NOT NULL UNIQUE,
                simhash             TEXT    NOT NULL DEFAULT '0000000000000000',
                title_tokens        TEXT    NOT NULL DEFAULT '',
                source_count        INTEGER NOT NULL DEFAULT 1,
                trend_score         REAL    NOT NULL DEFAULT 0.0,
                is_reported         INTEGER NOT NULL DEFAULT 0 CHECK (is_reported IN (0, 1)),
                reported_at         TEXT,
                telegram_message_id INTEGER,
                first_seen_at       TEXT    NOT NULL,
                crawled_at          TEXT    NOT NULL
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_canonical ON news(canonical_url)",
            "CREATE INDEX IF NOT EXISTS idx_news_category_reported ON news(category, is_reported)",
            "CREATE INDEX IF NOT EXISTS idx_news_trend ON news(category, trend_score DESC)",
            "CREATE INDEX IF NOT EXISTS idx_news_published ON news(published_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_news_hash ON news(content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_news_simhash ON news(simhash)",
            """
            CREATE TABLE IF NOT EXISTS news_sources (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id      INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
                source       TEXT    NOT NULL,
                url          TEXT    NOT NULL UNIQUE,
                author       TEXT,
                published_at TEXT,
                discovered_at TEXT   NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_news_sources_news ON news_sources(news_id)",
            """
            CREATE TABLE IF NOT EXISTS crawl_runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at          TEXT NOT NULL,
                finished_at         TEXT,
                sources_total       INTEGER NOT NULL DEFAULT 0,
                sources_ok          INTEGER NOT NULL DEFAULT 0,
                sources_failed      INTEGER NOT NULL DEFAULT 0,
                articles_found      INTEGER NOT NULL DEFAULT 0,
                new_articles        INTEGER NOT NULL DEFAULT 0,
                duplicates          INTEGER NOT NULL DEFAULT 0,
                telegram_sent       INTEGER NOT NULL DEFAULT 0,
                telegram_failed     INTEGER NOT NULL DEFAULT 0,
                error_message       TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS telegram_deliveries (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id       INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
                chat_id       TEXT    NOT NULL,
                message_id    INTEGER,
                sent_at       TEXT    NOT NULL,
                ok            INTEGER NOT NULL DEFAULT 1,
                error         TEXT
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_news_chat ON telegram_deliveries(news_id, chat_id)",
        ),
    ),
    (
        2,
        "add_engagement_columns",
        (
            "ALTER TABLE news ADD COLUMN engagement_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE news ADD COLUMN engagement_score REAL NOT NULL DEFAULT 0.0",
        ),
    ),
    (
        3,
        "add_skipped_flag",
        (
            "ALTER TABLE news ADD COLUMN is_skipped INTEGER NOT NULL DEFAULT 0 CHECK (is_skipped IN (0, 1))",
            "CREATE INDEX IF NOT EXISTS idx_news_delivery_state ON news(is_reported, is_skipped)",
        ),
    ),
    (
        4,
        "add_app_settings",
        (
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
)


class Database:
    """Bọc một kết nối aiosqlite duy nhất, tự khởi tạo và migrate schema."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Vòng đời
    # ------------------------------------------------------------------

    async def connect(self) -> aiosqlite.Connection:
        """Mở kết nối (idempotent), bật PRAGMA và chạy migration."""
        async with self._lock:
            if self._connection is None:
                logger.debug("Mở kết nối SQLite: %s", self.path)
                connection = await aiosqlite.connect(self.path)
                connection.row_factory = aiosqlite.Row
                for pragma in PRAGMAS:
                    await connection.execute(pragma)
                await connection.commit()
                self._connection = connection
            return self._connection

    async def close(self) -> None:
        """Đóng kết nối nếu đang mở."""
        async with self._lock:
            if self._connection is not None:
                logger.debug("Đóng kết nối SQLite")
                await self._connection.close()
                self._connection = None

    async def initialize(self) -> None:
        """Tạo bảng + chạy toàn bộ migration chưa áp dụng."""
        connection = await self.connect()
        async with self._write_lock:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    INTEGER PRIMARY KEY,
                    name       TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            await connection.commit()
            cursor = await connection.execute("SELECT version FROM schema_migrations")
            applied = {row[0] for row in await cursor.fetchall()}
            await cursor.close()

            for version, name, statements in MIGRATIONS:
                if version in applied:
                    continue
                logger.info("Áp dụng migration v%d: %s", version, name)
                try:
                    for statement in statements:
                        await connection.execute(statement)
                    await connection.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                        (version, name),
                    )
                    await connection.commit()
                except Exception as exc:  # noqa: BLE001 - cần rollback và log rõ
                    await connection.rollback()
                    logger.error("Migration v%d (%s) thất bại: %s", version, name, exc)
                    raise

    # ------------------------------------------------------------------
    # Truy vấn
    # ------------------------------------------------------------------

    async def fetch_all(self, sql: str, params: tuple | dict = ()) -> list[aiosqlite.Row]:
        connection = await self.connect()
        async with self._lock:
            cursor = await connection.execute(sql, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

    async def fetch_one(self, sql: str, params: tuple | dict = ()) -> aiosqlite.Row | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def fetch_value(self, sql: str, params: tuple | dict = (), default: object = None) -> object:
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    async def execute(self, sql: str, params: tuple | dict = ()) -> int:
        """Chạy 1 câu lệnh ghi, tự commit. Trả về lastrowid."""
        connection = await self.connect()
        async with self._write_lock:
            cursor = await connection.execute(sql, params)
            try:
                await connection.commit()
                return int(cursor.lastrowid or 0)
            finally:
                await cursor.close()

    async def execute_many(self, sql: str, seq_of_params: list[tuple | dict]) -> None:
        """Chạy nhiều câu lệnh ghi trong MỘT transaction."""
        if not seq_of_params:
            return
        connection = await self.connect()
        async with self._write_lock:
            try:
                await connection.executemany(sql, seq_of_params)
                await connection.commit()
            except Exception as exc:  # noqa: BLE001 - rollback rồi log
                await connection.rollback()
                logger.error("Lỗi SQLite khi ghi hàng loạt: %s", exc)
                raise

    class _Transaction:
        """Context manager cho transaction ghi, dùng thẳng connection."""

        def __init__(self, database: "Database") -> None:
            self._database = database
            self._connection: aiosqlite.Connection | None = None

        async def __aenter__(self) -> aiosqlite.Connection:
            await self._database._write_lock.acquire()
            self._connection = await self._database.connect()
            return self._connection

        async def __aexit__(self, exc_type, exc, tb) -> None:
            connection = self._connection
            try:
                if connection is not None:
                    if exc_type is None:
                        await connection.commit()
                    else:
                        await connection.rollback()
            finally:
                self._database._write_lock.release()

    def transaction(self) -> "Database._Transaction":
        """Mở transaction ghi (commit khi thoát bình thường, rollback khi lỗi)."""
        return Database._Transaction(self)
