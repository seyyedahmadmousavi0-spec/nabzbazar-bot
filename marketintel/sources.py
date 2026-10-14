"""Source registry: configuration lives in the database; no code change needed to add a source."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import audit, log_event
from .util import iso, jdump, utcnow

FIELDS = ("name", "source_type", "access_method", "enabled", "categories", "parser", "default_unit", "reliability",
          "chat_ref", "attribution", "url_template", "restrictions", "retention_days", "rate_limit_per_min",
          "poll_interval_s", "config")


def upsert_source(conn: sqlite3.Connection, key: str, **kw: Any) -> int:
    row = conn.execute("SELECT id FROM sources WHERE key=?", (key,)).fetchone()
    vals = {k: kw[k] for k in FIELDS if k in kw}
    for jf in ("categories", "config"):
        if jf in vals and not isinstance(vals[jf], str):
            vals[jf] = jdump(vals[jf])
    if "enabled" in vals:
        vals["enabled"] = int(bool(vals["enabled"]))
    if row:
        if vals:
            sets = ",".join(f"{k}=?" for k in vals)
            conn.execute(f"UPDATE sources SET {sets} WHERE id=?", (*vals.values(), row["id"]))
        return int(row["id"])
    vals.setdefault("name", key)
    vals.setdefault("source_type", "telegram_channel")
    vals.setdefault("access_method", "bot_api")
    cols = ["key", "created_at", *vals]
    cur = conn.execute(f"INSERT INTO sources({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                       (key, iso(utcnow()), *vals.values()))
    return int(cur.lastrowid or 0)


def load_sources_file(conn: sqlite3.Connection, path: str | Path) -> list[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    keys = []
    for s in data["sources"]:
        s = dict(s)
        key = s.pop("key")
        upsert_source(conn, key, **s)
        keys.append(key)
    return keys


def load_categories(conn: sqlite3.Connection, categories: list[dict[str, Any]]) -> None:
    for c in categories:
        conn.execute("INSERT OR IGNORE INTO categories(key,name_fa,stale_minutes) VALUES (?,?,?)",
                     (c["key"], c["name_fa"], c.get("stale_minutes", 360)))


def enabled_sources(conn: sqlite3.Connection, access_method: str | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM sources WHERE enabled=1"
    args: tuple[Any, ...] = ()
    if access_method:
        q += " AND access_method=?"
        args = (access_method,)
    return conn.execute(q, args).fetchall()


def set_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool, actor: str = "system") -> None:
    conn.execute("UPDATE sources SET enabled=? WHERE id=?", (int(enabled), source_id))
    audit(conn, actor, "source_enable" if enabled else "source_disable", {"source_id": source_id})


def mark_success(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("UPDATE sources SET last_success_at=?, health='ok', consecutive_failures=0, last_error=NULL WHERE id=?",
                 (iso(utcnow()), source_id))


def mark_failure(conn: sqlite3.Connection, source_id: int, error: str) -> None:
    from .logging_setup import redact
    conn.execute(
        "UPDATE sources SET last_error=?, last_error_at=?, consecutive_failures=consecutive_failures+1, "
        "health=CASE WHEN consecutive_failures+1>=3 THEN 'failing' ELSE 'degraded' END WHERE id=?",
        (redact(error)[:500], iso(utcnow()), source_id))
    log_event(conn, "ERROR", "ingest", f"source {source_id} failed: {redact(error)[:300]}")
