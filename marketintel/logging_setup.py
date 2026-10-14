"""Structured JSON logging with correlation ids and secret redaction."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

_SECRET_PATTERNS = [
    re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"(?i)(password|secret|token|api_hash|session)(\"?\s*[:=]\s*\"?)[^\s\",}]+"),
]


def redact(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub("bot<REDACTED>", text)
    text = _SECRET_PATTERNS[1].sub("<REDACTED>", text)
    return _SECRET_PATTERNS[2].sub(r"\1\2<REDACTED>", text)


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "cid": correlation_id.get(),
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level.upper())
    logging.getLogger("urllib3").setLevel(logging.WARNING)
