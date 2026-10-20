"""Logging setup shared by every backend module.

Uses ``loguru`` (pretty console output + rotating file log). Import
:func:`setup_logging` once in ``main.py``; other modules just do
``from loguru import logger``.

Every sink is wrapped in a redacting writer, so a credential cannot reach disk or
the terminal even by accident - including from a traceback, which is formatted
*after* the record is built and would therefore slip past a record-level filter.
That is the whole reason the rotation logic below exists instead of using
loguru's built-in ``rotation=`` (which only works when the sink is a path, and a
path bypasses the wrapper).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from .config import BACKEND_DIR, LogConfig
from .security import redact

_LEVELS = ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]

#: Everything the WebSocket layer sends each client is printed at this level.
WIRE_LEVEL = "TRACE"

#: One file, rotated at this size, keeping this many older copies.
_MAX_LOG_BYTES = 10 * 1024 * 1024
_LOG_BACKUPS = 5

_configured = False


class _RedactingWriter:
    """Wraps a text sink and scrubs credentials from every line it receives."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, message: str) -> None:
        self._stream.write(redact(message))

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:  # pragma: no cover - a closed stderr must not raise
            pass

    def stop(self) -> None:  # pragma: no cover - loguru calls it on removal
        self.flush()


class _RotatingRedactingFile:
    """A redacting sink that rotates by size.

    Deliberately small: append, flush, and rename when the file gets too big. The
    cost is that loguru's ``enqueue=`` (async, process-safe writes) is not
    available; this app is a single process, and flushing per line is what keeps
    the log readable during a crash.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = _MAX_LOG_BYTES,
        backups: int = _LOG_BACKUPS,
        encoding: str = "utf-8",
    ) -> None:
        self._path = path
        self._max = max_bytes
        self._backups = backups
        self._encoding = encoding
        self._fh: Any = None
        self._size = 0

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding=self._encoding)
        try:
            self._size = self._path.stat().st_size
        except OSError:
            self._size = 0

    def _rotate(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        oldest = Path(f"{self._path}.{self._backups}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self._backups - 1, 0, -1):
            src = Path(f"{self._path}.{index}")
            if src.exists():
                src.replace(Path(f"{self._path}.{index + 1}"))
        if self._path.exists():
            self._path.replace(Path(f"{self._path}.1"))
        self._open()

    def write(self, message: str) -> None:
        if self._fh is None:
            self._open()
        text = redact(message)
        if self._size > 0 and self._size + len(text.encode(self._encoding, "ignore")) > self._max:
            self._rotate()
        self._fh.write(text)
        self._fh.flush()
        self._size += len(text.encode(self._encoding, "ignore"))

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def stop(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class _InterceptHandler(logging.Handler):
    """Route ``logging`` records (uvicorn, httpx, ...) into loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _resolve(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else BACKEND_DIR / path


def setup_logging(config: LogConfig) -> None:
    """Idempotently configure console + file logging."""
    global _configured

    logger.remove()

    logger.add(
        _RedactingWriter(sys.stderr),
        level=config.level,
        colorize=config.colorize and sys.stderr.isatty(),
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
        ),
        backtrace=config.level in ("TRACE", "DEBUG"),
        diagnose=False,
    )

    logger.add(
        _RotatingRedactingFile(_resolve(config.file)),
        level="DEBUG",
        backtrace=True,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    )

    # Optional machine-readable sink: one JSON object per line, so a log
    # aggregator (or a test) can read events without parsing the pretty format.
    if json_target := (config.json_file or "").strip():
        json_path = _resolve(json_target)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            _RotatingRedactingFile(json_path),
            level=config.level,
            serialize=True,
        )
        logger.debug("structured log sink -> {}", json_path)

    # Let uvicorn / httpx / asyncio log through loguru as well.
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "websockets"):
        std = logging.getLogger(name)
        std.handlers = [_InterceptHandler()]
        std.propagate = False

    _configured = True
    logger.debug("logging configured (level={}, redaction=on)", config.level)


__all__ = ["WIRE_LEVEL", "logger", "setup_logging"]
