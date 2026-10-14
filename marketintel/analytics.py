"""Historical queries and market analytics. Only comparable observations are compared
(same entity_key, same canonical unit)."""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .util import iso, parse_iso

HISTORY_STATUSES = ("valid", "stale")


@dataclass
class Change:
    base_id: int
    base_amount: float
    base_observed_at: str
    abs_change: float
    pct_change: float
    window_minutes: int


def history(conn: sqlite3.Connection, entity_key: str, since: datetime | None = None,
            source_id: int | None = None, until: datetime | None = None, limit: int = 500,
            statuses: tuple[str, ...] = HISTORY_STATUSES) -> list[sqlite3.Row]:
    q = ("SELECT r.*, s.name AS source_name FROM records r JOIN sources s ON s.id=r.source_id "
         "WHERE r.entity_key=? AND r.status IN (%s) AND r.amount_canonical IS NOT NULL" % ",".join("?" * len(statuses)))
    args: list[Any] = [entity_key, *statuses]
    if since:
        q += " AND r.observed_at>=?"
        args.append(iso(since))
    if until:
        q += " AND r.observed_at<=?"
        args.append(iso(until))
    if source_id is not None:
        q += " AND r.source_id=?"
        args.append(source_id)
    q += " ORDER BY r.observed_at DESC, r.id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(q, args).fetchall()


def latest_by_source(conn: sqlite3.Connection, entity_key: str, now: datetime,
                     max_age_minutes: int | None = None,
                     statuses: tuple[str, ...] = ("valid", "stale")) -> list[dict[str, Any]]:
    rows = history(conn, entity_key, statuses=statuses)
    seen: dict[int, sqlite3.Row] = {}
    for r in rows:
        seen.setdefault(r["source_id"], r)
    out = []
    for r in seen.values():
        age = (now - parse_iso(r["observed_at"])).total_seconds() / 60
        d = dict(r)
        d["age_minutes"] = age
        d["is_stale"] = max_age_minutes is not None and age > max_age_minutes
        out.append(d)
    return out


def change_over(conn: sqlite3.Connection, rec: sqlite3.Row | dict[str, Any], window_minutes: int,
                tolerance: float = 2.0) -> Change | None:
    """Change vs the same source's observation closest to (observed_at - window).
    Returns None (no invented trend) when no suitable base observation exists."""
    t = parse_iso(rec["observed_at"])
    target = t - timedelta(minutes=window_minutes)
    lo = target - timedelta(minutes=window_minutes * (tolerance - 1))
    rows = history(conn, rec["entity_key"], since=lo, until=target + timedelta(minutes=window_minutes * 0.25),
                   source_id=rec["source_id"], statuses=("valid",))
    rows = [r for r in rows if r["id"] != rec["id"] and r["canonical_unit"] == rec["canonical_unit"]]
    if not rows:
        return None
    base = min(rows, key=lambda r: abs((parse_iso(r["observed_at"]) - target).total_seconds()))
    if not base["amount_canonical"]:
        return None
    diff = rec["amount_canonical"] - base["amount_canonical"]
    return Change(base["id"], base["amount_canonical"], base["observed_at"], diff,
                  diff / base["amount_canonical"] * 100, window_minutes)


def corroborating(conn: sqlite3.Connection, rec: sqlite3.Row | dict[str, Any], now: datetime,
                  tolerance_pct: float = 3.0, max_age_minutes: int = 180,
                  include_unconfirmed: bool = False) -> list[dict[str, Any]]:
    """Other *sources* whose latest valid observation agrees within tolerance. Copies are excluded
    because duplicates are stored with status 'duplicate' and never appear in history."""
    out = []
    st = ("valid", "stale", "needs_review") if include_unconfirmed else ("valid", "stale")
    for o in latest_by_source(conn, rec["entity_key"], now, max_age_minutes, st):
        if o["status"] == "needs_review" and "unconfirmed_jump" not in (o["validation"] or ""):
            continue
        if o["source_id"] == rec["source_id"] or o["is_stale"] or o["id"] == rec["id"]:
            continue
        if o["canonical_unit"] != rec["canonical_unit"] or not rec["amount_canonical"]:
            continue
        if abs(o["amount_canonical"] - rec["amount_canonical"]) / rec["amount_canonical"] * 100 <= tolerance_pct:
            out.append(o)
    return out


def robust_outlier(values: list[float], x: float, min_n: int = 5, k: float = 8.0) -> bool:
    """Median/MAD outlier test; needs min_n observations, never fires on thin data."""
    if len(values) < min_n:
        return False
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values]) or max(abs(med) * 0.001, 1e-9)
    return abs(x - med) / (1.4826 * mad) > k


def daily_summary(conn: sqlite3.Connection, category: str, now: datetime, hours: int = 24) -> list[dict[str, Any]]:
    since = now - timedelta(hours=hours)
    keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT entity_key FROM records WHERE category=? AND status='valid' AND observed_at>=? "
        "AND amount_canonical IS NOT NULL", (category, iso(since)))]
    out = []
    for key in keys:
        for src in {r["source_id"] for r in history(conn, key, since=since)}:
            rows = history(conn, key, since=since, source_id=src)
            rows = [r for r in rows if r["status"] == "valid"]
            if not rows:
                continue
            latest, first = rows[0], rows[-1]
            n = len(rows)
            item = {
                "entity_key": key, "source_id": src, "source_name": latest["source_name"],
                "asset_name": latest["asset_name"], "latest": latest, "n_obs": n,
                "high": max(r["amount_canonical"] for r in rows), "low": min(r["amount_canonical"] for r in rows),
                "pct_change": None, "abs_change": None,
            }
            if n >= 2 and first["amount_canonical"]:
                item["abs_change"] = latest["amount_canonical"] - first["amount_canonical"]
                item["pct_change"] = item["abs_change"] / first["amount_canonical"] * 100
            out.append(item)
    return out


def source_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Measurable reliability indicators per source. Reported only; never auto-applied."""
    out = []
    for s in conn.execute("SELECT * FROM sources ORDER BY id"):
        c = dict(conn.execute(
            "SELECT COUNT(*) total, SUM(status='valid') valid, SUM(status='invalid') invalid, "
            "SUM(status='needs_review') review, SUM(status='duplicate') dup, SUM(status='rejected') rejected, "
            "SUM(status='stale') stale FROM records WHERE source_id=?", (s["id"],)).fetchone())
        raw = conn.execute("SELECT COUNT(*) n, SUM(parse_status='unparsed') un FROM raw_messages WHERE source_id=?",
                           (s["id"],)).fetchone()
        lags = [(parse_iso(r["ingested_at"]) - parse_iso(r["observed_at"])).total_seconds() / 60
                for r in conn.execute("SELECT observed_at, ingested_at FROM records WHERE source_id=? "
                                      "ORDER BY id DESC LIMIT 200", (s["id"],))]
        total = c["total"] or 0
        out.append({
            "source": s["name"], "source_id": s["id"], "records": total, "raw_messages": raw["n"] or 0,
            "valid_rate": (c["valid"] or 0) / total if total else None,
            "invalid_rate": (c["invalid"] or 0) / total if total else None,
            "review_rate": (c["review"] or 0) / total if total else None,
            "duplicate_rate": (c["dup"] or 0) / total if total else None,
            "rejected_by_admin": c["rejected"] or 0,
            "unparsed_messages": raw["un"] or 0,
            "median_lag_minutes": statistics.median(lags) if lags else None,
            "configured_reliability": s["reliability"], "health": s["health"],
        })
    return out
