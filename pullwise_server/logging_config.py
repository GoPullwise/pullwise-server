from __future__ import annotations

import datetime
import logging
import os
from typing import TextIO


DEFAULT_LOG_ROTATION_TIME = datetime.time(0, 0)


class DailyDatedFileHandler(logging.Handler):
    terminator = "\n"

    def __init__(
        self,
        directory: str,
        *,
        prefix: str = "pullwise",
        rotation_time: datetime.time = DEFAULT_LOG_ROTATION_TIME,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        self.directory = directory
        self.prefix = prefix
        self.rotation_time = rotation_time
        self.encoding = encoding
        self._stream: TextIO | None = None
        self._current_log_date: datetime.date | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = self._stream_for_record(record)
            stream.write(self.format(record) + self.terminator)
            stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream:
                self._stream.close()
                self._stream = None
        finally:
            super().close()

    def _stream_for_record(self, record: logging.LogRecord) -> TextIO:
        log_date = self.log_date_for_timestamp(record.created)
        if self._stream and self._current_log_date == log_date:
            return self._stream

        if self._stream:
            self._stream.close()
            self._stream = None

        os.makedirs(self.directory, exist_ok=True)
        self._stream = open(self.path_for_date(log_date), "a", encoding=self.encoding)
        self._current_log_date = log_date
        return self._stream

    def log_date_for_timestamp(self, created: float) -> datetime.date:
        recorded_at = datetime.datetime.fromtimestamp(created)
        log_date = recorded_at.date()
        if self.rotation_time != DEFAULT_LOG_ROTATION_TIME and recorded_at.time() < self.rotation_time:
            log_date = log_date - datetime.timedelta(days=1)
        return log_date

    def path_for_date(self, log_date: datetime.date) -> str:
        return os.path.join(self.directory, f"{self.prefix}-{log_date.isoformat()}.log")


def configure_logging(*, project_root: str) -> None:
    log_dir = os.environ.get("PULLWISE_LOG_DIR") or os.path.join(project_root, ".pullwise", "logs")
    level = parse_log_level(os.environ.get("PULLWISE_LOG_LEVEL", "INFO"))
    rotation_time = parse_rotation_time(os.environ.get("PULLWISE_LOG_ROTATION_TIME", "00:00"))
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    remove_pullwise_handlers(root_logger)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler._pullwise_managed = True  # type: ignore[attr-defined]

    file_handler = DailyDatedFileHandler(log_dir, rotation_time=rotation_time)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler._pullwise_managed = True  # type: ignore[attr-defined]

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


def remove_pullwise_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_pullwise_managed", False):
            logger.removeHandler(handler)
            handler.close()


def parse_log_level(value: str) -> int:
    normalized = value.strip().upper()
    if normalized.isdigit():
        return int(normalized)
    level = logging.getLevelName(normalized)
    if isinstance(level, int):
        return level
    return logging.INFO


def parse_rotation_time(value: str) -> datetime.time:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        return datetime.time(hour, minute)
    except (TypeError, ValueError):
        return DEFAULT_LOG_ROTATION_TIME
