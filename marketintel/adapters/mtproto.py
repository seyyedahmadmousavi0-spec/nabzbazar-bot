"""Optional MTProto (Telethon) adapter for channels the configured account may legitimately read.

NOT exercised against real Telegram in this repository (needs api_id/api_hash, a session and
channel access). `message_to_raw` is unit-tested with fake objects; `poll` is a thin wrapper.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ..pipeline import RawInput, ingest_raw, process_raw
from ..sources import enabled_sources, mark_failure, mark_success
from ..util import UTC


class NotConfigured(RuntimeError):
    pass


def message_to_raw(msg: Any, username: str | None) -> RawInput:
    text = getattr(msg, "message", None) or getattr(msg, "text", "") or ""
    date: datetime = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=UTC)
    url = f"https://t.me/{username}/{msg.id}" if username else None
    fwd = getattr(msg, "fwd_from", None)
    forward = None
    if fwd is not None and getattr(fwd, "from_id", None) is not None:
        forward = str(getattr(fwd.from_id, "channel_id", ""))
    return RawInput(external_id=str(msg.id), text=text, message_date=date.astimezone(UTC), url=url,
                    forward_from=forward, raw={"edited": getattr(msg, "edit_date", None) is not None})


def poll_once(conn: sqlite3.Connection, api_id: int, api_hash: str, session_path: str,
              now: datetime | None = None, tzname: str = "Asia/Tehran", limit: int = 50) -> dict[str, int]:
    try:
        from telethon.sync import TelegramClient  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotConfigured("telethon is not installed (pip install telethon)") from exc
    stats = {"new": 0, "edit": 0, "unchanged": 0}
    sources = enabled_sources(conn, "mtproto")
    if not sources:
        return stats
    with TelegramClient(session_path, api_id, api_hash) as client:
        for s in sources:
            try:
                min_id = int(s["cursor"] or 0)
                top = min_id
                # paced, bounded reads; Telethon sleeps on small FloodWaits by itself
                for msg in reversed(list(client.iter_messages(s["chat_ref"], min_id=min_id, limit=limit))):
                    raw_id, kind = ingest_raw(conn, s["id"], message_to_raw(msg, (s["chat_ref"] or "").lstrip("@")), now)
                    stats[kind] += 1
                    if kind != "unchanged":
                        process_raw(conn, raw_id, now, tzname=tzname)
                    top = max(top, msg.id)
                conn.execute("UPDATE sources SET cursor=? WHERE id=?", (str(top), s["id"]))
                mark_success(conn, s["id"])
            except Exception as exc:
                mark_failure(conn, s["id"], f"{type(exc).__name__}: {exc}")
    return stats
