"""Application logging with token redaction and bounded local files."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from job_notis.config import redact_secret
from job_notis.paths import AppPaths


class _SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secret(record.msg)
        if record.args:
            record.args = tuple(
                redact_secret(value) if isinstance(value, str) else value for value in record.args
            )
        return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secret(super().format(record))


def configure_logging(paths: AppPaths, *, console: bool = True, verbose: bool = False) -> None:
    """Configure bounded local logs, falling back to stderr when unavailable."""

    logger = logging.getLogger("job_notis")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.propagate = False

    formatter = _RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redaction = _SecretRedactionFilter()

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.addFilter(redaction)
        logger.addHandler(console_handler)

    try:
        paths.ensure_runtime_directories()
        file_handler = RotatingFileHandler(
            paths.log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    except OSError as exc:
        if not logger.handlers:
            fallback_handler = logging.StreamHandler()
            fallback_handler.setFormatter(formatter)
            fallback_handler.addFilter(redaction)
            logger.addHandler(fallback_handler)
        logger.warning("File logging is unavailable: %s", exc)
        return

    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction)
    logger.addHandler(file_handler)
