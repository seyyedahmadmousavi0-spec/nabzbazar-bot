"""Outgoing Telegram delivery (Bot API) plus safe dry-run / mock senders."""
from __future__ import annotations

import itertools
import logging
from typing import Any, Protocol

import requests

from .logging_setup import redact

log = logging.getLogger(__name__)


class SendError(Exception):
    pass


class RateLimited(SendError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class TransientError(SendError):
    pass


class PermanentError(SendError):
    pass


class Sender(Protocol):
    def send(self, chat_id: str, text: str, buttons: list[list[dict[str, str]]]) -> str: ...


class TelegramSender:
    def __init__(self, token: str, session: requests.Session | Any | None = None,
                 base_url: str = "https://api.telegram.org", timeout: float = 20.0):
        if not token:
            raise ValueError("bot token required")
        self._token = token
        self._s = session or requests.Session()
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def send(self, chat_id: str, text: str, buttons: list[list[dict[str, str]]]) -> str:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
                                   "link_preview_options": {"is_disabled": True}}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            r = self._s.post(f"{self._base}/bot{self._token}/sendMessage", json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            raise TransientError(redact(f"network error: {type(exc).__name__}")) from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 200 and body.get("ok"):
            return str(body["result"]["message_id"])
        desc = redact(str(body.get("description", r.reason or "")))[:200]
        if r.status_code == 429:
            raise RateLimited(float(body.get("parameters", {}).get("retry_after", 5)))
        if r.status_code >= 500:
            raise TransientError(f"telegram {r.status_code}: {desc}")
        raise PermanentError(f"telegram {r.status_code}: {desc}")


class ConsoleSender:
    """Default when MI_LIVE_PUBLISH is not 1: nothing leaves the machine."""

    def __init__(self) -> None:
        self._n = itertools.count(1)
        self.sent: list[tuple[str, str]] = []

    def send(self, chat_id: str, text: str, buttons: list[list[dict[str, str]]]) -> str:
        self.sent.append((chat_id, text))
        log.info("DRY-RUN publish to %s (%d chars)", chat_id or "<unset>", len(text))
        return f"dry-{next(self._n)}"


class MockSender:
    """Test double with scripted failures: each item is an exception instance or None (success)."""

    def __init__(self, script: list[Exception | None] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []
        self._n = itertools.count(100)

    def send(self, chat_id: str, text: str, buttons: list[list[dict[str, str]]]) -> str:
        self.calls.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        if self.script:
            e = self.script.pop(0)
            if e is not None:
                raise e
        return str(next(self._n))
