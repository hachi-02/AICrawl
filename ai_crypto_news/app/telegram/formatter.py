"""Định dạng nội dung gửi Telegram (plain text + emoji, không dùng parse_mode)."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime

from app.database.models import Category, NewsRecord
from app.dedupe.normalizer import hours_since
from app.logging_config import get_logger

logger = get_logger(__name__)

TELEGRAM_MAX_LENGTH = 4096
SEPARATOR = "\n" + "─" * 24 + "\n"

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"[ \t]+")

MAX_SUMMARY_CHARS = 700
MAX_TITLE_CHARS = 300


def sanitize(text: str) -> str:
    """Bỏ ký tự điều khiển, gọn khoảng trắng, bỏ ký tự mô tả Unicode lạ."""
    if not text:
        return ""
    cleaned = _CONTROL_RE.sub(" ", text)
    cleaned = _WS_RE.sub(" ", cleaned)
    cleaned = "".join(char for char in cleaned if char == "\n" or char.isprintable())
    return cleaned.strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Cắt chuỗi theo ranh giới từ, thêm dấu … nếu bị cắt."""
    if not text or len(text) <= limit:
        return text
    clipped = text[: max(0, limit - len(suffix))]
    space = clipped.rfind(" ")
    if space > limit * 0.6:
        clipped = clipped[:space]
    return clipped.rstrip() + suffix


def relative_time(value: datetime | None, now: datetime) -> str:
    """Thời gian tương đối bằng tiếng Việt: 'vừa xong', '15 phút trước', '2 ngày trước'."""
    if value is None:
        return "vừa xong"
    age = hours_since(value, now)
    if not math.isfinite(age) or age <= 0.002:  # dưới ~7 giây
        return "vừa xong"
    minutes = age * 60
    if minutes < 1:  # dưới 1 phút -> "vừa xong", tránh hiện "0 phút trước"
        return "vừa xong"
    if minutes < 60:
        return f"{int(minutes)} phút trước"
    if age < 24:
        return f"{int(age)} giờ trước"
    days = int(age // 24)
    if days < 30:
        return f"{days} ngày trước"
    months = days // 30
    if months < 12:
        return f"{months} tháng trước"
    return f"{days // 365} năm trước"


def _format_item(index: int, record: NewsRecord, now: datetime, show_score: bool) -> str:
    lines: list[str] = []
    title = truncate(sanitize(record.title), MAX_TITLE_CHARS)
    header = f"{index}. {title}"
    if show_score:
        header += f"  [{record.trend_score:.2f}]"
    lines.append(header)

    meta = [f"   Nguồn: {sanitize(record.source)}", f"   🕒 {relative_time(record.published_at or record.first_seen_at, now)}"]
    if record.source_count > 1:
        meta.append(f"   📰 {record.source_count} nguồn cùng đưa tin")
    lines.append(" ".join(meta))

    summary = sanitize(record.summary or "")
    if summary:
        lines.append("")
        lines.append(f"Tóm tắt:\n{truncate(summary, MAX_SUMMARY_CHARS)}")

    lines.append("")
    lines.append(f"🔗 {record.url}")
    return "\n".join(lines)


def format_trending(
    category: Category | None,
    records: Sequence[NewsRecord],
    now: datetime,
    show_score: bool = True,
) -> str:
    """Tin nổi bật theo chủ đề, đúng format yêu cầu."""
    if not records:
        scope = category.label if category else "AI và Crypto"
        return f"🔥 TRENDING {scope}\n\nChưa có tin nào đạt ngưỡng nổi bật."

    blocks: list[str] = []
    groups: dict[Category, list[NewsRecord]] = {}
    for record in records:
        groups.setdefault(record.category, []).append(record)

    order = [Category.AI, Category.CRYPTO] if category is None else [category]
    for group in order:
        items = groups.get(group)
        if not items:
            continue
        # Điểm cao lên đầu; cùng điểm thì tin mới hơn lên trước.
        # Mốc tối thiểu phải có tzinfo để so sánh được với datetime có timezone.
        oldest = datetime.min.replace(tzinfo=UTC)
        items = sorted(
            items,
            key=lambda r: (r.trend_score, r.published_at or r.first_seen_at or oldest),
            reverse=True,
        )
        head = f"🔥 TRENDING {group.label}"
        if len(records) > 1:
            head += f"  ({len(items)} tin)"
        body = [head, ""]
        for index, record in enumerate(items, start=1):
            body.append(_format_item(index, record, now, show_score))
            if index != len(items):
                body.append(SEPARATOR)
        blocks.append("\n".join(body))

    return "\n\n".join(blocks)


def format_latest(
    records: Sequence[NewsRecord],
    now: datetime,
    title: str = "🆕 TIN MỚI NHẤT",
) -> str:
    """Danh sách tin mới nhất (dùng cho /latest, /ai, /crypto)."""
    if not records:
        return f"{title}\n\nChưa có tin nào trong database. Hãy chạy /crawl trước."

    blocks = [title, ""]
    for index, record in enumerate(records, start=1):
        blocks.append(_format_item(index, record, now, show_score=False))
        if index != len(records):
            blocks.append(SEPARATOR)
    return "\n".join(blocks)


def format_status(
    stats: dict[str, object],
    settings_summary: dict[str, object],
    now: datetime,
) -> str:
    """Báo cáo trạng thái crawler + database cho lệnh /status."""
    by_category = stats.get("by_category") or {}
    category_text = " | ".join(
        f"{Category(key).label}: {value}" for key, value in dict(by_category).items()
    )
    last_run = stats.get("last_run")
    if isinstance(last_run, dict):
        started = last_run.get("started_at")
        found = last_run.get("articles_found", 0)
        new = last_run.get("new_articles", 0)
        dupes = last_run.get("duplicates", 0)
        sent = last_run.get("telegram_sent", 0)
        failed = last_run.get("telegram_failed", 0)
        run_text = (
            f"• Lần crawl gần nhất: {started}\n"
            f"   Tìm thấy {found} | Mới {new} | Trùng {dupes}\n"
            f"   Telegram gửi {sent} | Lỗi {failed}"
        )
    else:
        run_text = "• Chưa có lần crawl nào"

    sources_text = "\n".join(
        f"   - {_source_name(row)}: {_source_total(row)}"
        for row in (stats.get("top_sources") or [])  # type: ignore[union-attr]
    ) or "   (chưa có dữ liệu)"

    size_mb = int(stats.get("db_size_bytes") or 0) / 1024 / 1024
    return (
        "📊 TRẠNG THÁI HỆ THỐNG\n"
        "━━━━━━━━━━━━━━━\n"
        f"🕒 {now.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
        f"🗄 DATABASE ({size_mb:.2f} MB)\n"
        f"• Tổng tin: {stats.get('total', 0)}\n"
        f"• {category_text}\n"
        f"• Đã gửi Telegram: {stats.get('reported', 0)}\n"
        f"• Đã bỏ qua: {stats.get('skipped', 0)}\n"
        f"• Chưa gửi: {stats.get('unreported', 0)}\n"
        f"• Tin 24h qua: {stats.get('last_24h', 0)}\n"
        f"• Số lượt nguồn: {stats.get('source_mentions', 0)} (bảng news_sources: {stats.get('extra_sources', 0)})\n\n"
        f"🌐 TOP NGUỒN\n{sources_text}\n\n"
        f"⚙️ CẤU HÌNH\n"
        + "\n".join(f"• {key}: {value}" for key, value in settings_summary.items())
        + f"\n\n{run_text}"
    )


def _source_name(row: object) -> str:
    """``top_sources`` có thể là tuple ``(source, total)`` hoặc dict của sqlite.Row."""
    if isinstance(row, dict):
        return str(row.get("source", "?"))
    if isinstance(row, (tuple, list)) and row:
        return str(row[0])
    return str(row)


def _source_total(row: object) -> object:
    if isinstance(row, dict):
        return row.get("total", 0)
    if isinstance(row, (tuple, list)) and len(row) > 1:
        return row[1]
    return 0


def format_crawl_result(
    stats: dict[str, object],
    duration_seconds: float,
) -> str:
    """Tóm tắt kết quả một vòng crawl (dùng sau /crawl và cho log)."""
    return (
        "✅ CRAWL HOÀN TẤT\n"
        "━━━━━━━━━━━━━━━\n"
        f"• Thời gian: {duration_seconds:.1f}s\n"
        f"• Nguồn OK / lỗi: {stats.get('sources_ok', 0)} / {stats.get('sources_failed', 0)}\n"
        f"• Tìm thấy: {stats.get('articles_found', 0)} bài\n"
        f"• Tin mới: {stats.get('new_articles', 0)}\n"
        f"• Trùng (đã bỏ qua): {stats.get('duplicates', 0)}\n"
        f"• Nguồn liên kết thêm: {stats.get('extra_sources_linked', 0)}\n"
        f"• Telegram đã gửi: {stats.get('telegram_sent', 0)}\n"
        f"• Telegram lỗi: {stats.get('telegram_failed', 0)}"
    )


def _hard_split(text: str, limit: int) -> list[str]:
    """Cắt cứng theo ký tự khi một "từ" dài hơn giới hạn (URL dài, token lạ...)."""
    return [text[start : start + limit] for start in range(0, len(text), limit)]


def split_message(text: str, limit: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Chia message dài quá giới hạn của Telegram theo ranh giới dòng/đoạn.

    Luôn đảm bảo mọi chunk có độ dài ``<= limit`` và không sinh chunk rỗng.
    """
    if limit <= 0:
        return []
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for line in text.splitlines(keepends=True):
        if len(buffer) + len(line) <= limit:
            buffer += line
            continue
        if buffer.strip():
            chunks.append(buffer.rstrip())
        buffer = ""
        if len(line) <= limit:
            buffer = line
            continue
        # Một dòng quá dài: cắt theo từ, từ nào dài quá limit thì cắt cứng
        piece = ""
        for word in line.split(" "):
            candidate = f"{piece}{word} " if piece else f"{word} "
            if len(candidate) <= limit:
                piece = candidate
                continue
            if piece.strip():
                chunks.append(piece.rstrip())
            if len(word) > limit:
                pieces = _hard_split(word, limit)
                chunks.extend(pieces[:-1])
                piece = pieces[-1] + " "
            else:
                piece = f"{word} "
        buffer = piece
    if buffer.strip():
        chunks.append(buffer.rstrip())
    return [chunk for chunk in chunks if chunk.strip()] or _hard_split(text, limit)


__all__ = [
    "SEPARATOR",
    "TELEGRAM_MAX_LENGTH",
    "format_crawl_result",
    "format_latest",
    "format_status",
    "format_trending",
    "relative_time",
    "sanitize",
    "split_message",
    "truncate",
]
