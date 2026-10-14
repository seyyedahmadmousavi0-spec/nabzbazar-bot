"""Telegram Web Channel Ingestion.

Scrapes public channels via Telegram's public web preview (https://t.me/s/<channel>).
Requires no bot admin privileges and no user account login.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from html import unescape
from typing import Any

import requests

from ..pipeline import RawInput, ingest_raw, process_raw
from ..sources import enabled_sources, mark_failure, mark_success
from ..util import UTC

log = logging.getLogger(__name__)

_POST_BLOCK_RE = re.compile(
    r'<div[^>]*class="[^"]*tgme_widget_message\b[^"]*"[^>]*data-post="([^"]+)"(.*?)'
    r'(?=<div[^>]*class="[^"]*tgme_widget_message\b|\Z)',
    re.DOTALL
)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_TEXT_RE = re.compile(r'<div[^>]*class="[^"]*tgme_widget_message_text\b[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_FWD_RE = re.compile(r'<a[^>]*class="[^"]*tgme_widget_message_forwarded_from_name[^"]*"[^>]*href="https://t.me/([^"]+)"')


def extract_web_messages(html: str, username: str) -> list[RawInput]:
    """Parse HTML from https://t.me/s/<username> into RawInput objects."""
    results: list[RawInput] = []
    for match in _POST_BLOCK_RE.finditer(html):
        post_attr = match.group(1)
        block = match.group(2)
        
        msg_id_str = post_attr.split("/")[-1] if "/" in post_attr else post_attr
        try:
            msg_id_int = int(msg_id_str)
        except ValueError:
            continue
            
        time_m = _TIME_RE.search(block)
        dt = None
        if time_m:
            try:
                dt = datetime.fromisoformat(time_m.group(1))
            except Exception:
                pass
        if dt is None:
            dt = datetime.now(timezone.utc)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
            
        text_m = _TEXT_RE.search(block)
        if not text_m:
            continue
            
        raw_html = text_m.group(1)
        cleaned = re.sub(r'<br\s*/?>', '\n', raw_html)
        cleaned = re.sub(r'<[^>]+>', '', cleaned)
        cleaned = unescape(cleaned).strip()
        if not cleaned:
            continue
            
        fwd_m = _FWD_RE.search(block)
        forward = f"@{fwd_m.group(1)}" if fwd_m else None
        
        url = f"https://t.me/{username}/{msg_id_int}"
        results.append(RawInput(
            external_id=str(msg_id_int),
            text=cleaned,
            message_date=dt,
            url=url,
            forward_from=forward,
            raw={"edited": "tgme_widget_message_edited" in block}
        ))
        
    results.sort(key=lambda r: int(r.external_id))
    return results


def poll_source(conn: sqlite3.Connection, source_row: sqlite3.Row, session: requests.Session | None = None,
                now: datetime | None = None, tzname: str = "Asia/Tehran", timeout: int = 15) -> dict[str, int]:
    s = session or requests.Session()
    stats = {"new": 0, "edit": 0, "unchanged": 0}
    chat_ref = (source_row["chat_ref"] or "").lstrip("@")
    if not chat_ref:
        return stats
        
    url = f"https://t.me/s/{chat_ref}"
    resp = s.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=timeout)
    if resp.status_code != 200:
        raise ConnectionError(f"HTTP {resp.status_code} fetching {url}")
        
    messages = extract_web_messages(resp.text, chat_ref)
    cursor = int(source_row["cursor"] or 0)
    top = cursor
    
    # If cursor is 0 (first run), take the last 10 messages to avoid flooding
    if cursor == 0 and len(messages) > 10:
        messages = messages[-10:]
        
    for raw in messages:
        msg_id = int(raw.external_id)
        if cursor > 0 and msg_id <= cursor:
            continue
        raw_id, kind = ingest_raw(conn, source_row["id"], raw, now)
        stats[kind] += 1
        if kind != "unchanged":
            process_raw(conn, raw_id, now, tzname=tzname)
        top = max(top, msg_id)
        
    conn.execute("UPDATE sources SET cursor=? WHERE id=?", (str(top), source_row["id"]))
    mark_success(conn, source_row["id"])
    return stats


def poll_once(conn: sqlite3.Connection, now: datetime | None = None, tzname: str = "Asia/Tehran",
              session: requests.Session | None = None, timeout: int = 15) -> dict[str, int]:
    totals = {"sources": 0, "new": 0, "edit": 0, "unchanged": 0}
    sources = enabled_sources(conn, "telegram_web")
    if not sources:
        # Also check sources with access_method="web"
        sources = enabled_sources(conn, "web")
    if not sources:
        return totals
        
    s = session or requests.Session()
    for src in sources:
        totals["sources"] += 1
        try:
            st = poll_source(conn, src, session=s, now=now, tzname=tzname, timeout=timeout)
            for k in ("new", "edit", "unchanged"):
                totals[k] += st[k]
        except Exception as exc:
            log.warning("failed polling source %s: %s", src["key"], exc)
            mark_failure(conn, src["id"], f"{type(exc).__name__}: {exc}")
            
    return totals

