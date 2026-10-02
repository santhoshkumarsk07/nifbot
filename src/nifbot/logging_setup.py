"""Structured JSON logging with secret redaction, rotation and an audit log."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from nifbot.timeutil import IST

REDACTED = "***REDACTED***"
AUDIT_LOGGER = "nifbot.audit"

# Patterns that look like secrets even if they were never registered.
_PATTERNS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"(?i)(bot)\d{6,12}:[A-Za-z0-9_-]{30,}"),  # token inside Bot API URL
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(r"(?i)((?:access[_-]?token|api[_-]?key|secret|password)\s*[=:]\s*)\S+"),
)


class Redactor:
    """Replaces known secret values and secret-looking patterns with a marker."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            if pattern.groups:
                text = pattern.sub(lambda m: (m.group(1) or "") + REDACTED, text)
            else:
                text = pattern.sub(REDACTED, text)
        return text


class RedactingFilter(logging.Filter):
    """Logging filter that renders the message and redacts it in place."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redact = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = self._redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            record.fields = {k: self._redact(str(v)) for k, v in fields.items()}
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, timestamps in IST."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=IST).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(
    log_dir: Path,
    secrets: Iterable[str] = (),
    level: str = "INFO",
    max_bytes: int = 5_000_000,
    backup_count: int = 10,
    console: bool = True,
) -> Redactor:
    """Configure root and audit loggers. Returns the redactor in use."""
    log_dir.mkdir(parents=True, exist_ok=True)
    redactor = Redactor(secrets)
    redact_filter = RedactingFilter(redactor)
    formatter = JsonFormatter()

    def handler(name: str) -> RotatingFileHandler:
        path = log_dir / name
        h = RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        path.chmod(0o600)
        h.setFormatter(formatter)
        h.addFilter(redact_filter)
        return h

    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.setLevel(level)
    root.addHandler(handler("nifbot.log"))
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(redact_filter)
        root.addHandler(stream)

    audit = logging.getLogger(AUDIT_LOGGER)
    for old in list(audit.handlers):
        audit.removeHandler(old)
    audit.setLevel(logging.INFO)
    audit.addHandler(handler("audit.log"))

    # httpx logs full request URLs at INFO; Bot API URLs contain the token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return redactor


def audit(event: str, **fields: Any) -> None:
    """Write an audit record (calls sent, button presses, rejected access...)."""
    logging.getLogger(AUDIT_LOGGER).info(event, extra={"fields": {"event": event, **fields}})
