"""Duplicate / syndication detection. Distinct entities (different variant, year, city, side...)
have different entity_keys and are therefore never merged."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .util import iso


def tokens(norm_text: str) -> set[str]:
    return set(re.findall(r"[\w\u0600-\u06FF]+", norm_text))


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_duplicate(conn: sqlite3.Connection, *, entity_key: str, category: str, amount: float | None,
                   source_id: int, raw_id: int, norm_text: str, forward_from: str | None,
                   observed_at: datetime, settings: dict[str, Any],
                   exclude_id: int | None = None) -> tuple[int, str] | None:
    window = timedelta(days=14 if category in ("car", "deal") else 0, hours=0 if category in ("car", "deal") else 6)
    rows = conn.execute(
        "SELECT r.id, r.amount, r.source_id, r.raw_id, r.observed_at, w.norm_text, s.chat_ref, s.key AS skey "
        "FROM records r JOIN raw_messages w ON w.id=r.raw_id JOIN sources s ON s.id=r.source_id "
        "WHERE r.entity_key=? AND r.status IN ('valid','needs_review','stale') AND r.observed_at BETWEEN ? AND ? "
        "AND r.id<>COALESCE(?,-1) ORDER BY r.observed_at ASC, r.id ASC",
        (entity_key, iso(observed_at - window), iso(observed_at + timedelta(minutes=1)), exclude_id)).fetchall()
    if not rows:
        return None
    repeat = timedelta(minutes=int(settings["repeat_window_minutes"]))
    thr = float(settings["dup_similarity"])
    min_tok = int(settings["dup_min_tokens"])
    tol = float(settings["price_tolerance_car_pct"]) if category == "car" else 0.0
    my_tokens = len(tokens(norm_text))

    def price_close(a: float | None, t: float) -> bool:
        if amount is None or a is None:
            return amount is None and a is None
        return abs(a - amount) <= abs(a) * t / 100.0

    from .util import parse_iso
    for r in rows:
        same_src = r["source_id"] == source_id
        # same-source reposts must carry the identical price; tolerance only applies across sources
        if not price_close(r["amount"], 0.0 if same_src else tol):
            continue
        dt = abs(parse_iso(r["observed_at"]) - observed_at)
        if same_src and r["raw_id"] == raw_id:
            return r["id"], "same_message_repeat"
        if same_src and category not in ("car", "deal") and dt <= repeat:
            return r["id"], "same_source_repeat"
        if same_src and category in ("car", "deal"):
            if similarity(norm_text, r["norm_text"]) >= 0.6 or category == "deal":
                return r["id"], "repost_same_source"
        if not same_src:
            if forward_from and forward_from in (r["chat_ref"], r["skey"]):
                return r["id"], "forwarded_copy"
            if my_tokens >= min_tok and similarity(norm_text, r["norm_text"]) >= thr:
                return r["id"], "syndicated_copy"
    return None
