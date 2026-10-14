"""Transparent, configurable opportunity detection. Scores are heuristic and uncalibrated."""
from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timedelta
from typing import Any

from . import analytics
from .db import get_setting
from .textnorm import to_persian_digits
from .util import iso, jdump, jload, parse_iso

ASSUMPTIONS = {
    "market": "اختلاف قیمت به معنی سود تضمین‌شده نیست؛ کارمزد، مالیات، هزینه حمل، موقعیت، نقدشوندگی و تفاوت زمان ثبت لحاظ نشده است.",
    "car": "مقایسه فقط بر اساس برند/مدل/تیپ/سال/شهر یکسان انجام شده؛ کارکرد، رنگ، وضعیت بدنه و شرایط پرداخت لحاظ نشده است.",
    "deal": "درصد تخفیف نسبت به قیمت مرجع ذکرشده محاسبه شده؛ اگر مرجع «اعلام‌شده توسط فروشنده» باشد، تأیید مستقل ندارد.",
}


def score(components: dict[str, float], weights: dict[str, float]) -> tuple[float, list[dict[str, Any]]]:
    total_w = sum(weights.values()) or 1.0
    br, total = [], 0.0
    for k, w in weights.items():
        c = max(0.0, min(1.0, components.get(k, 0.0)))
        total += w * c
        br.append({"component": k, "value": round(c, 3), "weight": w, "contribution": round(100 * w * c / total_w, 1)})
    return round(100 * total / total_w, 1), br


def _obs(conn: sqlite3.Connection, r: Any, label: str) -> dict[str, Any]:
    raw = conn.execute("SELECT url FROM raw_messages WHERE id=?", (r["raw_id"],)).fetchone()
    s = conn.execute("SELECT name FROM sources WHERE id=?", (r["source_id"],)).fetchone()
    amt = r["amount_canonical"]
    disp = f"{amt / 10:,.0f} تومان" if r["canonical_unit"] == "IRR" else f"{amt:,.2f} USD"
    return {"label": label, "record_id": r["id"], "value": to_persian_digits(disp), "source": s["name"],
            "observed_at": r["observed_at"], "url": raw["url"] if raw else None,
            "confidence": r["confidence"]}


def _insert(conn: sqlite3.Connection, *, kind: str, rec: Any, title: str, explanation: str, assump: str,
            evidence: dict[str, Any], comps: dict[str, float], dedupe_key: str, now: datetime,
            expires_at: datetime | None = None) -> int | None:
    sc, br = score(comps, get_setting(conn, "opportunity_weights"))
    cur = conn.execute(
        "INSERT OR IGNORE INTO opportunities(kind,category,entity_key,title,explanation,assumptions,evidence,score,"
        "score_breakdown,status,dedupe_key,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
        (kind, rec["category"], rec["entity_key"], title, explanation, assump, jdump(evidence), sc, jdump(br),
         dedupe_key, iso(now), iso(expires_at) if expires_at else None))
    return int(cur.lastrowid) if cur.rowcount else None


def detect_for_record(conn: sqlite3.Connection, rec: Any, now: datetime) -> list[int]:
    out: list[int | None] = []
    cat = rec["category"]
    thr_spread = float(get_setting(conn, "spread_threshold_pct"))
    thr_drop = float(get_setting(conn, "drop_threshold_pct"))
    min_obs = int(get_setting(conn, "history_min_obs"))
    hour = now.strftime("%Y%m%d%H")
    if cat in ("currency", "gold", "coin", "crypto"):
        stale = int((conn.execute("SELECT stale_minutes FROM categories WHERE key=?", (cat,)).fetchone() or [360])[0])
        latest = [x for x in analytics.latest_by_source(conn, rec["entity_key"], now, stale) if not x["is_stale"]
                  and x["canonical_unit"] == rec["canonical_unit"] and x["confidence"] >= 0.5]
        if len(latest) >= 2:
            lo = min(latest, key=lambda x: x["amount_canonical"])
            hi = max(latest, key=lambda x: x["amount_canonical"])
            spread = (hi["amount_canonical"] - lo["amount_canonical"]) / lo["amount_canonical"] * 100
            if spread >= thr_spread:
                depth = len(analytics.history(conn, rec["entity_key"], since=now - timedelta(days=7)))
                comps = {"magnitude": spread / (thr_spread * 4), "confidence": min(lo["confidence"], hi["confidence"]),
                         "freshness": 1 - max(lo["age_minutes"], hi["age_minutes"]) / stale,
                         "corroboration": 0.0, "depth": depth / (min_obs * 3)}
                out.append(_insert(
                    conn, kind="cross_source_spread", rec=rec,
                    title=f"اختلاف قیمت {rec['asset_name']} بین منابع",
                    explanation=f"قیمت‌های ثبت‌شده‌ی دو منبع برای «{rec['asset_name']}» حدود {to_persian_digits(f'{spread:.1f}')}٪ اختلاف دارد.",
                    assump=ASSUMPTIONS["market"],
                    evidence={"spread_pct": round(spread, 2), "observations": [_obs(conn, lo, "کمترین"), _obs(conn, hi, "بیشترین")]},
                    comps=comps, dedupe_key=f"spread:{rec['entity_key']}:{hour}", now=now))
        ch = analytics.change_over(conn, rec, int(get_setting(conn, "alert_window_minutes")))
        if ch and ch.pct_change <= -thr_drop:
            base = conn.execute("SELECT * FROM records WHERE id=?", (ch.base_id,)).fetchone()
            comps = {"magnitude": abs(ch.pct_change) / (thr_drop * 4), "confidence": rec["confidence"],
                     "freshness": 1.0, "corroboration": 1.0 if analytics.corroborating(conn, rec, now) else 0.0,
                     "depth": len(analytics.history(conn, rec["entity_key"], since=now - timedelta(days=2))) / (min_obs * 3)}
            out.append(_insert(
                conn, kind="price_drop", rec=rec, title=f"کاهش قیمت {rec['asset_name']}",
                explanation=f"قیمت نسبت به مشاهده‌ی قبلی همان منبع {to_persian_digits(f'{abs(ch.pct_change):.1f}')}٪ کاهش یافته است.",
                assump=ASSUMPTIONS["market"],
                evidence={"change_pct": round(ch.pct_change, 2), "window_minutes": ch.window_minutes,
                          "observations": [_obs(conn, base, "مشاهده‌ی قبلی"), _obs(conn, rec, "مشاهده‌ی فعلی")]},
                comps=comps, dedupe_key=f"drop:{rec['entity_key']}:{rec['id']}", now=now))
    elif cat == "car" and rec["amount_canonical"]:
        rows = [r for r in analytics.history(conn, rec["entity_key"], since=now - timedelta(days=14))
                if r["id"] != rec["id"] and r["canonical_unit"] == rec["canonical_unit"]]
        if len(rows) >= min_obs:
            med = statistics.median(r["amount_canonical"] for r in rows)
            below = (med - rec["amount_canonical"]) / med * 100
            if below >= thr_drop * 3:
                comps = {"magnitude": below / (thr_drop * 12), "confidence": rec["confidence"], "freshness": 1.0,
                         "corroboration": 0.0, "depth": len(rows) / (min_obs * 3)}
                out.append(_insert(
                    conn, kind="car_below_comparables", rec=rec,
                    title=f"آگهی {rec['asset_name']} {rec['model'] or ''} پایین‌تر از آگهی‌های مشابه",
                    explanation=f"قیمت آگهی حدود {to_persian_digits(f'{below:.1f}')}٪ کمتر از میانه‌ی {to_persian_digits(str(len(rows)))} آگهی مشابه است.",
                    assump=ASSUMPTIONS["car"],
                    evidence={"median_canonical": med, "n_comparables": len(rows),
                              "observations": [_obs(conn, rec, "این آگهی")] + [_obs(conn, r, "مشابه") for r in rows[:2]]},
                    comps=comps, dedupe_key=f"carbelow:{rec['id']}", now=now))
    elif cat == "deal":
        ex = jload(rec["extras"], {})
        hours = ex.get("expires_in_hours")
        expires = parse_iso(rec["observed_at"]) + timedelta(hours=hours) if hours else None
        if expires and expires < now:
            return []
        ref, ref_kind, pct = None, None, None
        hist = [r for r in analytics.history(conn, rec["entity_key"], since=now - timedelta(days=90))
                if r["id"] != rec["id"]]
        if len(hist) >= min_obs and rec["amount_canonical"]:
            ref = statistics.median(r["amount_canonical"] for r in hist)
            ref_kind = "historical_median"
        elif ex.get("old_price") and rec["amount"]:
            ref = ex["old_price"] * (10 if rec["stated_unit"] == "IRT" else 1)
            ref_kind = "seller_stated_unverified"
        if ref and rec["amount_canonical"]:
            pct = (ref - rec["amount_canonical"]) / ref * 100
        if pct is not None and pct >= float(get_setting(conn, "deal_min_discount_pct")):
            credible = ref_kind == "historical_median"
            comps = {"magnitude": pct / 60, "confidence": rec["confidence"] * (1.0 if credible else 0.6),
                     "freshness": 1.0, "corroboration": 0.0, "depth": len(hist) / (min_obs * 3)}
            out.append(_insert(
                conn, kind="deal_discount", rec=rec, title=f"تخفیف: {rec['asset_name']}",
                explanation=(f"قیمت حدود {to_persian_digits(f'{pct:.0f}')}٪ پایین‌تر از "
                             + ("میانه‌ی قیمت‌های ثبت‌شده‌ی قبلی" if credible else "قیمت قبلیِ اعلام‌شده توسط فروشنده (تأییدنشده)") + " است."),
                assump=ASSUMPTIONS["deal"],
                evidence={"reference_kind": ref_kind, "reference_canonical": ref, "discount_pct": round(pct, 1),
                          "observations": [_obs(conn, rec, "پیشنهاد")]},
                comps=comps, dedupe_key=f"deal:{rec['entity_key']}:{rec['amount']}", now=now, expires_at=expires))
        elif expires and not pct:
            out.append(_insert(
                conn, kind="limited_offer", rec=rec, title=f"پیشنهاد محدودالزمان: {rec['asset_name']}",
                explanation="این پیشنهاد دارای مهلت محدود است و قبلاً در کانال منتشر نشده است.",
                assump=ASSUMPTIONS["deal"], evidence={"observations": [_obs(conn, rec, "پیشنهاد")]},
                comps={"magnitude": 0.3, "confidence": rec["confidence"], "freshness": 1.0, "corroboration": 0.0,
                       "depth": 0.0},
                dedupe_key=f"limited:{rec['entity_key']}:{rec['amount']}", now=now, expires_at=expires))
    valid_out = [i for i in out if i]
    if get_setting(conn, "auto_approve_opportunities") == 1 or get_setting(conn, "publish_mode") == "auto_all":
        from . import publisher
        for opp_id in valid_out:
            opp = conn.execute("SELECT * FROM opportunities WHERE id=?", (opp_id,)).fetchone()
            if opp and opp["status"] == "pending":
                text, buttons = publisher.render_opportunity(conn, opp, "Asia/Tehran")
                ev = jload(opp["evidence"], {})
                rids = [o["record_id"] for o in ev.get("observations", []) if "record_id" in o]
                publisher.enqueue(conn, kind="opportunity", category=opp["category"], text=text,
                                  buttons=buttons, key=f"opp:{opp_id}", record_ids=rids,
                                  confidence=0.9, now=now, ref=("opportunity", opp_id), approved_by="auto:system")
                conn.execute("UPDATE opportunities SET status='approved', decided_by='auto:system', decided_at=? WHERE id=?",
                             (iso(now), opp_id))
    return valid_out


def expire_old(conn: sqlite3.Connection, now: datetime) -> int:
    return conn.execute("UPDATE opportunities SET status='expired' WHERE status IN ('pending','approved') "
                        "AND expires_at IS NOT NULL AND expires_at<?", (iso(now),)).rowcount
