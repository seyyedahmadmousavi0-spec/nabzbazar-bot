from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from marketintel import db
from marketintel.catalog import default_catalog
from marketintel.pipeline import RawInput, ingest_raw, process_raw
from marketintel.sources import load_categories, upsert_source
from marketintel.util import UTC

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def make_db() -> sqlite3.Connection:
    c = db.connect(":memory:")
    db.migrate(c)
    load_categories(c, default_catalog().categories)
    db.set_setting(c, "min_interval_seconds", 0)
    return c


def add_source(c: sqlite3.Connection, key: str = "a", **kw) -> int:
    kw.setdefault("name", key.upper())
    kw.setdefault("reliability", 0.9)
    kw.setdefault("chat_ref", f"@{key}")
    kw.setdefault("url_template", "https://t.me/{username}/{message_id}")
    return upsert_source(c, key, **kw)


def post(c: sqlite3.Connection, sid: int, ext: str, text: str, minutes_ago: float = 0, now: datetime = NOW,
         forward_from: str | None = None) -> list[int]:
    when = now - timedelta(minutes=minutes_ago)
    raw_id, kind = ingest_raw(c, sid, RawInput(ext, text, when, forward_from=forward_from), now)
    return process_raw(c, raw_id, now) if kind != "unchanged" else []


def rec(c: sqlite3.Connection, rid: int) -> sqlite3.Row:
    return c.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
