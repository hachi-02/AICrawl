"""Cấu hình logging: ghi ra console và file xoay vòng (RotatingFileHandler)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.config import Settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED = False


def _force_utf8_stdio() -> None:
    """Ép stdout/stderr dùng UTF-8 (Windows mặc định cp1252 nên không in được tiếng Việt)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - stream đã bị đóng
                pass


def setup_logging(settings: Settings) -> None:
    """Khởi tạo logging một lần duy nhất cho toàn process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    _force_utf8_stdio()
    settings.ensure_directories()
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(Path(settings.resolved_log_file)),
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # playwright spam log debug khi bật trace
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Lấy logger theo tên module."""
    return logging.getLogger(name)
