"""SQLite access, migrations, settings, logging helpers."""
from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .util import iso, jdump, utcnow

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

DEFAULT_SETTINGS: dict[str, Any] = {
    "publish_mode": "manual",          # manual | auto | auto_categories
    "paused": 0,                       # emergency pause of ALL automated publishing
    "auto_categories": [],             # used by auto_categories mode
    "min_auto_confidence": 0.8,
    "auto_approve_opportunities": 0,   # 1 = auto publish opportunities without approval
    "auto_publish_quotes": 0,          # 1 = auto publish regular price updates
    "quiet_hours": "",                 # "23:00-07:00" in publication timezone; empty = off
    "min_interval_seconds": 3,
    "alert_change_pct": 2.0,
    "alert_window_minutes": 60,
    "spread_threshold_pct": 2.0,
    "drop_threshold_pct": 3.0,
    "unusual_jump_pct": 15.0,
    "repeat_window_minutes": 10,
    "dup_similarity": 0.7,
    "dup_min_tokens": 6,
    "price_tolerance_car_pct": 1.0,
    "history_min_obs": 3,
    "display_unit": "toman",
    "brand_line": "📈 بازار‌یار",
    "opportunity_weights": {"magnitude": 0.45, "confidence": 0.2, "freshness": 0.15,
                            "corroboration": 0.1, "depth": 0.1},
    "deal_min_discount_pct": 15.0,
    "summary_hours_local": [9, 21],
    "_bot_api_offset": 0,
    "max_attempts": 6,
    "lease_seconds": 120,
    "show_source": 1,
    "show_source_url": 1,
    "show_verification": 1,
    "thousands_separator": "٬",
    "auto_bulletin_interval_minutes": 0,
}


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit write transaction (autocommit mode connection)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied: list[str] = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.stem in done:
            continue
        conn.executescript("BEGIN;\n" + f.read_text(encoding="utf-8") + "\n;")
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?,?)", (f.stem, iso(utcnow())))
        conn.execute("COMMIT") if conn.in_transaction else None
        applied.append(f.stem)
    return applied


# ---- settings ---------------------------------------------------------------------
def get_setting(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS[key]
    return json.loads(row[0])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(f"unknown setting {key}")
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))


def all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    return {k: get_setting(conn, k) for k in DEFAULT_SETTINGS}


# ---- logging into DB --------------------------------------------------------------
def log_event(conn: sqlite3.Connection, level: str, component: str, message: str,
              correlation_id: str | None = None, **context: Any) -> None:
    conn.execute(
        "INSERT INTO event_log(ts,level,component,message,correlation_id,context) VALUES (?,?,?,?,?,?)",
        (iso(utcnow()), level, component, message[:2000], correlation_id, jdump(context) if context else None),
    )


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: Any = None) -> None:
    conn.execute("INSERT INTO audit_log(ts,actor,action,detail) VALUES (?,?,?,?)",
                 (iso(utcnow()), actor, action, jdump(detail) if detail is not None else None))
