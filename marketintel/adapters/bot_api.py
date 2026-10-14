"""Bot API ingestion. The bot only sees channels where it is a member/admin and cannot read history.

Bot API limitations (documented in docs/architecture.md): no history backfill, no deletion events for
channels, a single getUpdates consumer per bot (not compatible with an active webhook).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any, Protocol

import requests

from ..db import get_setting, log_event, set_setting, tx
from ..logging_setup import redact
from ..pipeline import RawInput, ingest_raw, process_raw
from ..sources import enabled_sources, mark_failure, mark_success
from ..util import UTC

log = logging.getLogger(__name__)
ALLOWED = ["channel_post", "edited_channel_post"]


class BotClient(Protocol):
    def get_updates(self, offset: int, timeout: int = 0) -> list[dict[str, Any]]: ...


class HttpBotClient:
    def __init__(self, token: str, session: Any | None = None, base_url: str = "https://api.telegram.org"):
        self._token, self._s, self._base = token, session or requests.Session(), base_url.rstrip("/")

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict[str, Any]]:
        try:
            r = self._s.post(f"{self._base}/bot{self._token}/getUpdates", timeout=timeout + 15,
                             json={"offset": offset, "timeout": timeout, "allowed_updates": ALLOWED})
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise ConnectionError(redact(f"getUpdates failed: {type(exc).__name__}")) from None
        if not body.get("ok"):
            raise ConnectionError(redact(f"getUpdates error {r.status_code}: {body.get('description', '')}")[:200])
        return list(body["result"])


class FixtureBotClient:
    """Serves Bot-API-shaped updates from memory (tests/demo). Honours offset like the real API."""

    def __init__(self, updates: list[dict[str, Any]], fail_times: int = 0):
        self.updates, self.fail_times = updates, fail_times

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict[str, Any]]:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("simulated network failure")
        return [u for u in self.updates if u["update_id"] >= offset]


def _match_source(sources: list[sqlite3.Row], chat: dict[str, Any]) -> sqlite3.Row | None:
    cid = str(chat.get("id", ""))
    uname = ("@" + chat["username"]).lower() if chat.get("username") else None
    for s in sources:
        ref = (s["chat_ref"] or "").lower()
        if ref and ref in (cid, uname):
            return s
    return None


def update_to_raw(upd: dict[str, Any]) -> tuple[dict[str, Any], RawInput] | None:
    msg = upd.get("channel_post") or upd.get("edited_channel_post")
    if not msg:
        return None
    text = msg.get("text") or msg.get("caption") or ""
    chat = msg["chat"]
    origin = msg.get("forward_origin") or {}
    fchat = (origin.get("chat") or {})
    forward = ("@" + fchat["username"]) if fchat.get("username") else (str(fchat["id"]) if fchat.get("id") else None)
    url = f"https://t.me/{chat['username']}/{msg['message_id']}" if chat.get("username") else None
    return chat, RawInput(external_id=str(msg["message_id"]), text=text,
                          message_date=datetime.fromtimestamp(msg["date"], UTC), url=url, forward_from=forward,
                          raw={"update_id": upd["update_id"], "has_media": any(
                              k in msg for k in ("photo", "document", "video", "animation")),
                              "edited": "edited_channel_post" in upd})


def poll_once(conn: sqlite3.Connection, client: BotClient, now: datetime | None = None,
              tzname: str = "Asia/Tehran") -> dict[str, int]:
    sources = enabled_sources(conn, "bot_api")
    stats = {"updates": 0, "new": 0, "edit": 0, "unchanged": 0, "unmapped": 0}
    if not sources:
        return stats
    offset = int(get_setting(conn, "_bot_api_offset"))
    try:
        updates = client.get_updates(offset)
    except ConnectionError as exc:
        for s in sources:
            mark_failure(conn, s["id"], str(exc))
        raise
    for upd in updates:
        stats["updates"] += 1
        parsed = update_to_raw(upd)
        if parsed:
            chat, raw = parsed
            src = _match_source(sources, chat)
            if src is None:
                stats["unmapped"] += 1
            else:
                raw_id, kind = ingest_raw(conn, src["id"], raw, now)
                stats[kind] += 1
                if kind != "unchanged":
                    process_raw(conn, raw_id, now, tzname=tzname)
        with tx(conn):  # advance offset only after the update was durably handled
            set_setting(conn, "_bot_api_offset", upd["update_id"] + 1)
    for s in sources:
        mark_success(conn, s["id"])
    if stats["unmapped"]:
        log_event(conn, "WARN", "ingest", f"{stats['unmapped']} update(s) from unconfigured chats ignored")
    return stats
