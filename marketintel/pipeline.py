"""Raw ingestion -> parse -> validate -> dedupe -> persist -> analytics hooks."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import dedupe, opportunities, publisher
from .catalog import Catalog, default_catalog
from .db import all_settings, audit, log_event, tx
from .logging_setup import new_correlation_id
from .parsing import ParsedItem, SourceCtx, parse_message
from .textnorm import normalize_for_match
from .util import iso, jdump, jload, utcnow

log = logging.getLogger(__name__)


@dataclass
class RawInput:
    external_id: str
    text: str
    message_date: datetime
    url: str | None = None
    forward_from: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _hash(norm: str) -> str:
    return hashlib.sha256(norm.encode()).hexdigest()


def ingest_raw(conn: sqlite3.Connection, source_id: int, inp: RawInput, now: datetime | None = None
               ) -> tuple[int, str]:
    """Idempotent. Returns (raw_id, 'new'|'edit'|'unchanged'). Edits create a new revision."""
    now = now or utcnow()
    norm = normalize_for_match(inp.text)
    h = _hash(norm)
    src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    url = inp.url
    if not url and src["url_template"]:
        user = (src["chat_ref"] or "").lstrip("@")
        url = src["url_template"].format(username=user, message_id=inp.external_id)
    with tx(conn):
        prev = conn.execute("SELECT * FROM raw_messages WHERE source_id=? AND external_id=? "
                            "ORDER BY revision DESC LIMIT 1", (source_id, inp.external_id)).fetchone()
        if prev and prev["text_hash"] == h and not prev["deleted_at"]:
            return int(prev["id"]), "unchanged"
        revision = prev["revision"] + 1 if prev else 0
        if prev:
            conn.execute("UPDATE raw_messages SET is_current=0 WHERE id=?", (prev["id"],))
            _supersede(conn, prev["id"], "message edited")
        cur = conn.execute(
            "INSERT INTO raw_messages(source_id,external_id,revision,text,norm_text,text_hash,raw_json,message_date,"
            "received_at,url,forward_from) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, inp.external_id, revision, inp.text, norm, h, jdump(inp.raw), iso(inp.message_date),
             iso(now), url, inp.forward_from))
    return int(cur.lastrowid or 0), ("edit" if prev else "new")


def _supersede(conn: sqlite3.Connection, raw_id: int, reason: str) -> None:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM records WHERE raw_id=? AND status NOT IN ('retracted','rejected')", (raw_id,))]
    if ids:
        conn.execute(f"UPDATE records SET status='superseded' WHERE id IN ({','.join('?' * len(ids))})", ids)
        publisher.cancel_for_records(conn, ids, reason)


def mark_deleted(conn: sqlite3.Connection, source_id: int, external_id: str, now: datetime | None = None) -> int:
    """For sources where deletions are observable (e.g. MTProto). Already-published posts are not unpublished."""
    now = now or utcnow()
    n = 0
    with tx(conn):
        for r in conn.execute("SELECT id FROM raw_messages WHERE source_id=? AND external_id=?",
                              (source_id, external_id)).fetchall():
            conn.execute("UPDATE raw_messages SET deleted_at=?, is_current=0 WHERE id=?", (iso(now), r["id"]))
            ids = [x["id"] for x in conn.execute("SELECT id FROM records WHERE raw_id=? AND status NOT IN "
                                                 "('rejected')", (r["id"],))]
            if ids:
                conn.execute(f"UPDATE records SET status='retracted' WHERE id IN ({','.join('?' * len(ids))})", ids)
                publisher.cancel_for_records(conn, ids, "source message deleted")
                n += len(ids)
    return n


def _insert_record(conn: sqlite3.Connection, raw: sqlite3.Row, src: sqlite3.Row, item: ParsedItem,
                   now: datetime, catalog: Catalog, settings: dict[str, Any]) -> tuple[int, str]:
    from .util import parse_iso
    from .validation import validate
    observed = item.observed_at or parse_iso(raw["message_date"])
    verdict = validate(conn, item, observed, now, src, catalog, settings)
    status, dup_of, dup_reason = verdict.status, None, None
    amount = float(item.amount) if item.amount is not None else None
    canon = float(item.amount_canonical) if item.amount_canonical is not None else None
    if status in ("valid", "needs_review", "stale"):
        d = dedupe.find_duplicate(conn, entity_key=item.entity_key, category=item.category, amount=amount,
                                  source_id=src["id"], raw_id=raw["id"], norm_text=raw["norm_text"],
                                  forward_from=raw["forward_from"], observed_at=observed, settings=settings)
        if d:
            status, (dup_of, dup_reason) = "duplicate", d
    cur = conn.execute(
        "INSERT INTO records(raw_id,source_id,category,asset_key,asset_name,brand,model,variant,model_year,side,"
        "amount,price_low,price_high,stated_unit,unit_rule,amount_canonical,canonical_unit,unit_of_measure,"
        "price_type,location,observed_at,observed_at_source,published_at,ingested_at,entity_key,extras,snippet,"
        "parse_method,confidence,confidence_detail,validation,status,dup_of,dup_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (raw["id"], src["id"], item.category, item.asset_key, item.asset_name, item.brand, item.model,
         item.variant, item.model_year, item.side, amount,
         float(item.price_low) if item.price_low is not None else None,
         float(item.price_high) if item.price_high is not None else None,
         item.stated_unit, item.unit_rule, canon, item.canonical_unit, item.unit_of_measure, item.price_type,
         item.location, iso(observed), item.observed_source, raw["message_date"], iso(now), item.entity_key,
         jdump(item.extras), item.snippet, item.method, verdict.confidence, jdump(verdict.confidence_detail),
         jdump(verdict.flags), status, dup_of, dup_reason))
    return int(cur.lastrowid or 0), status


def _promote_corroborated(conn: sqlite3.Connection, rid: int, now: datetime) -> list[int]:
    """A big move reported by two independent sources is no longer 'unconfirmed': release the earlier one."""
    from . import analytics
    row = conn.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    if "unusual_move_corroborated" not in (row["validation"] or ""):
        return []
    promoted = []
    for o in analytics.corroborating(conn, row, now, tolerance_pct=3.0, include_unconfirmed=True):
        if o["status"] == "needs_review":
            codes = [f["code"] for f in jload(o["validation"], [])]
            if set(codes) <= {"unconfirmed_jump", "unit_not_in_price_token"}:
                conn.execute("UPDATE records SET status='valid', reviewed_by='auto:corroborated', reviewed_at=? "
                             "WHERE id=? AND status='needs_review'", (iso(now), o["id"]))
                promoted.append(o["id"])
    return promoted


def process_raw(conn: sqlite3.Connection, raw_id: int, now: datetime | None = None,
                catalog: Catalog | None = None, tzname: str = "Asia/Tehran") -> list[int]:
    now = now or utcnow()
    catalog = catalog or default_catalog()
    cid = new_correlation_id()
    raw = conn.execute("SELECT * FROM raw_messages WHERE id=?", (raw_id,)).fetchone()
    src = conn.execute("SELECT * FROM sources WHERE id=?", (raw["source_id"],)).fetchone()
    from .util import parse_iso
    settings = all_settings(conn)
    ctx = SourceCtx(default_unit=src["default_unit"], categories=tuple(jload(src["categories"], [])),
                    timezone=tzname, message_date=parse_iso(raw["message_date"]))
    res = parse_message(raw["text"], src["parser"], ctx, catalog)
    ids: list[int] = []
    valid_ids: list[int] = []
    with tx(conn):
        conn.execute("UPDATE raw_messages SET parse_status=?, parse_note=? WHERE id=?",
                     (res.status, res.note, raw_id))
        for item in res.items:
            rid, status = _insert_record(conn, raw, src, item, now, catalog, settings)
            ids.append(rid)
            if status == "valid":
                valid_ids.append(rid)
                valid_ids += _promote_corroborated(conn, rid, now)
        log_event(conn, "INFO" if res.status != "unparsed" else "WARN", "pipeline",
                  f"raw {raw_id} {res.status}: {len(ids)} record(s) {res.note}", cid, source_id=src["id"])
        try:
            if settings.get("enable_car_announcements", 0):
                from .car_announcements import detect_car_announcement, render_car_announcement
                src_cats = jload(src["categories"], []) if src and src["categories"] else []
                is_car_source = "car" in src_cats or any(k in (raw["text"] or "") for k in (
                    "قیمت جدید", "پیش فروش", "فروش فوق العاده", "حساب وکالتی", "بخشنامه", "میزان افزایش"
                ))
                if is_car_source:
                    ann = detect_car_announcement(raw["text"], conn)
                    if ann:
                        brand = publisher._brand(conn)
                        ann_text = render_car_announcement(ann, brand)
                        ann_key = f"car_ann:{raw['source_id']}:{raw['external_id']}"
                        publisher.enqueue(conn, kind="car_announcement", category="car", text=ann_text,
                                          buttons=[], key=ann_key, record_ids=ids, confidence=1.0, now=now,
                                          ref=("raw", raw["id"]))
        except Exception as exc:
            log.exception("car announcement check failed: %s", exc)
    for rid in valid_ids:
        run_hooks(conn, rid, now, tzname)
    return ids


def run_hooks(conn: sqlite3.Connection, record_id: int, now: datetime, tzname: str) -> None:
    rec = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    if rec is None or rec["status"] != "valid":
        return
    try:
        with tx(conn):
            publisher.maybe_enqueue_alert(conn, rec, now, tzname)
            opportunities.detect_for_record(conn, rec, now)
    except Exception as exc:  # analytics must never lose the stored record
        log.exception("hooks failed for record %s", record_id)
        log_event(conn, "ERROR", "pipeline", f"hooks failed: {type(exc).__name__}: {exc}", record_id=record_id)


def review_record(conn: sqlite3.Connection, record_id: int, approve: bool, actor: str,
                  now: datetime | None = None, tzname: str = "Asia/Tehran") -> bool:
    now = now or utcnow()
    with tx(conn):
        cur = conn.execute("UPDATE records SET status=?, reviewed_by=?, reviewed_at=? WHERE id=? "
                           "AND status='needs_review'",
                           ("valid" if approve else "rejected", actor, iso(now), record_id))
        if cur.rowcount:
            audit(conn, actor, "record_approve" if approve else "record_reject", {"id": record_id})
    if cur.rowcount and approve:
        run_hooks(conn, record_id, now, tzname)
    return bool(cur.rowcount)


def reprocess_raw(conn: sqlite3.Connection, raw_id: int, actor: str, now: datetime | None = None,
                  tzname: str = "Asia/Tehran") -> list[int]:
    """Manual retry of a parse job: supersedes previous records of this raw message and re-runs the pipeline."""
    with tx(conn):
        _supersede(conn, raw_id, "reprocessed")
        audit(conn, actor, "raw_reprocess", {"raw_id": raw_id})
    return process_raw(conn, raw_id, now, tzname=tzname)


def decide_opportunity(conn: sqlite3.Connection, opp_id: int, approve: bool, actor: str, tzname: str,
                       now: datetime | None = None) -> bool:
    now = now or utcnow()
    with tx(conn):
        opp = conn.execute("SELECT * FROM opportunities WHERE id=? AND status='pending'", (opp_id,)).fetchone()
        if not opp:
            return False
        conn.execute("UPDATE opportunities SET status=?, decided_by=?, decided_at=? WHERE id=?",
                     ("approved" if approve else "rejected", actor, iso(now), opp_id))
        audit(conn, actor, "opportunity_approve" if approve else "opportunity_reject", {"id": opp_id})
        if approve:
            text, buttons = publisher.render_opportunity(conn, opp, tzname)
            ev = json.loads(opp["evidence"])
            rids = [o["record_id"] for o in ev.get("observations", [])]
            publisher.enqueue(conn, kind="opportunity", category=opp["category"], text=text, buttons=buttons,
                              key=f"opp:{opp_id}", record_ids=rids, confidence=None, now=now,
                              ref=("opportunity", opp_id), approved_by=actor)
    return True
