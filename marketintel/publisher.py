"""Formatting, transactional outbox, approval modes and delivery."""
from __future__ import annotations

import html
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import analytics
from .db import audit, get_setting, log_event, tx
from .jalali import to_jalali_date
from .senders import PermanentError, RateLimited, Sender, SendError
from .textnorm import to_persian_digits
from .util import iso, jdump, jload, parse_iso, tz, utcnow

log = logging.getLogger(__name__)
RLM = "\u200f"
MINUS = "\u2212"
MAX_LEN = 4000


# ---- formatting helpers ----------------------------------------------------------------
def e(s: Any) -> str:
    return html.escape(str(s), quote=False)


def fmt_number(v: float, decimals: int = 0, sep: str = "٬") -> str:
    s = f"{v:,.{decimals}f}" if decimals else f"{int(round(v)):,}"
    return to_persian_digits(s.replace(",", sep).replace(".", "٫"))


def fmt_amount(rec: Any, display_unit: str = "toman", sep: str = "٬") -> str:
    c = rec["amount_canonical"]
    if c is None:
        return "—"
    if rec["canonical_unit"] == "USD":
        return f"{fmt_number(c, 2 if c < 1000 else 0, sep=sep)} دلار"
    if display_unit == "toman":
        return f"{fmt_number(c / 10, sep=sep)} تومان"
    return f"{fmt_number(c, sep=sep)} ریال"


def fmt_time(iso_s: str, tzname: str) -> str:
    dt = parse_iso(iso_s).astimezone(tz(tzname))
    jy, jm, jd = to_jalali_date(dt)
    return to_persian_digits(f"{jy}/{jm:02d}/{jd:02d} - {dt:%H:%M}")


def fmt_change(pct: float | None, absv: float | None, canonical_unit: str, display_unit: str, window: str, sep: str = "٬") -> str | None:
    if pct is None or absv is None:
        return None
    arrow = "🔺" if pct > 0 else "🔻" if pct < 0 else "➖"
    sign = "+" if pct > 0 else MINUS if pct < 0 else ""
    a = abs(absv) / (10 if canonical_unit == "IRR" and display_unit == "toman" else 1)
    return f"{arrow} {sign}{fmt_number(a, 2 if canonical_unit == 'USD' and a < 1000 else 0, sep=sep)} ({sign}{fmt_number(abs(pct), 2, sep=sep)}٪) نسبت به {window}"


def _line(label: str, value: str) -> str:
    return f"{RLM}• {label}: {value}"


def _brand(conn: sqlite3.Connection) -> str:
    return f"{RLM}{e(get_setting(conn, 'brand_line'))}"


def _source_name(conn: sqlite3.Connection, rec: Any) -> str:
    s = conn.execute("SELECT name, attribution FROM sources WHERE id=?", (rec["source_id"],)).fetchone()
    return e(s["attribution"] or s["name"])


def _raw_url(conn: sqlite3.Connection, rec: Any) -> str | None:
    r = conn.execute("SELECT url FROM raw_messages WHERE id=?", (rec["raw_id"],)).fetchone()
    return r["url"] if r and r["url"] else None


def _url_button(url: str | None) -> list[list[dict[str, str]]]:
    return [[{"text": "مشاهده منبع اصلی", "url": url}]] if url and url.startswith(("http://", "https://")) else []


def _window_label(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{to_persian_digits(str(minutes // 1440))} روز قبل"
    if minutes % 60 == 0:
        return f"{to_persian_digits(str(minutes // 60))} ساعت قبل"
    return f"{to_persian_digits(str(minutes))} دقیقه قبل"


def verification_line(n_corroborating: int) -> str:
    if n_corroborating >= 1:
        return f"{RLM}• وضعیت: هم‌خوان با {to_persian_digits(str(n_corroborating))} منبع مستقل دیگر"
    return f"{RLM}• وضعیت: گزارش یک منبع (تأییدنشده توسط منبع دیگر)"


# ---- renderers ------------------------------------------------------------------------
def render_record(conn: sqlite3.Connection, rec: Any, tzname: str, change: analytics.Change | None = None,
                  n_corr: int = 0, title: str = "📊 <b>گزارش قیمت بازار</b>") -> tuple[str, list[list[dict[str, str]]]]:
    du = get_setting(conn, "display_unit")
    sep = get_setting(conn, "thousands_separator") or ","
    show_src = int(get_setting(conn, "show_source") if get_setting(conn, "show_source") is not None else 1)
    show_ver = int(get_setting(conn, "show_verification") if get_setting(conn, "show_verification") is not None else 1)
    show_url = int(get_setting(conn, "show_source_url") if get_setting(conn, "show_source_url") is not None else 1)

    cat = rec["category"]
    extras = jload(rec["extras"], {})
    lines = [f"{RLM}{title}", ""]
    if cat == "car":
        name = " ".join(x for x in [rec["asset_name"], rec["model"], rec["variant"]] if x)
        lines.append(_line("خودرو", e(name)))
        if rec["model_year"]:
            lines.append(_line("مدل", to_persian_digits(str(rec["model_year"]))))
        if rec["location"]:
            lines.append(_line("شهر", e(rec["location"])))
        lines.append(_line("قیمت آگهی (قیمت درخواستی)", e(fmt_amount(rec, du, sep=sep))))
    elif cat == "deal":
        lines.append(_line("محصول", e(rec["asset_name"])))
        lines.append(_line("قیمت با تخفیف (اعلام‌شده توسط فروشنده)", e(fmt_amount(rec, du, sep=sep))))
        if extras.get("old_price"):
            old = dict(rec)
            old["amount_canonical"] = extras["old_price"] * (10 if rec["stated_unit"] == "IRT" else 1)
            lines.append(_line("قیمت قبلی (اعلام‌شده)", e(fmt_amount(old, du, sep=sep))))
        if extras.get("discount_pct_stated"):
            lines.append(_line("تخفیف اعلام‌شده", f"{fmt_number(extras['discount_pct_stated'], sep=sep)}٪"))
        if extras.get("coupon_code"):
            lines.append(_line("کد تخفیف", f"<code>{e(extras['coupon_code'])}</code>"))
    else:
        side = {"buy": " (خرید)", "sell": " (فروش)"}.get(rec["side"] or "", "")
        lines.append(_line("دارایی", e((rec["asset_name"] or "") + side)))
        lines.append(_line("قیمت ثبت‌شده", e(fmt_amount(rec, du, sep=sep))))
        if change:
            ch = fmt_change(change.pct_change, change.abs_change, rec["canonical_unit"], du,
                            _window_label(change.window_minutes), sep=sep)
            if ch:
                lines.append(_line("تغییر", e(ch)))
    lines.append(_line("زمان مشاهده", e(fmt_time(rec["observed_at"], tzname))))
    if show_src:
        lines.append(_line("منبع", _source_name(conn, rec)))
    if show_ver:
        lines.append(verification_line(n_corr))
    url = _raw_url(conn, rec)
    if url and show_url:
        lines += ["", f"{RLM}🔗 <a href=\"{e(url)}\">مشاهده منبع اصلی</a>"]
    lines += ["", _brand(conn)]
    buttons = _url_button(url) if show_url else []
    return "\n".join(lines)[:MAX_LEN], buttons


def render_summary(conn: sqlite3.Connection, category: str, items: list[dict[str, Any]], tzname: str,
                   now: datetime, hours: int = 24) -> str | None:
    if not items:
        return None
    du = get_setting(conn, "display_unit")
    sep = get_setting(conn, "thousands_separator") or ","
    show_src = int(get_setting(conn, "show_source") if get_setting(conn, "show_source") is not None else 1)
    cname = conn.execute("SELECT name_fa FROM categories WHERE key=?", (category,)).fetchone()
    jy, jm, jd = to_jalali_date(now.astimezone(tz(tzname)))
    lines = [f"{RLM}📊 <b>خلاصه‌ی بازار {e(cname['name_fa'] if cname else category)}</b>",
             f"{RLM}{to_persian_digits(f'{jy}/{jm:02d}/{jd:02d}')}", ""]
    for it in sorted(items, key=lambda x: (x["asset_name"] or "", x["entity_key"]))[:25]:
        lt = it["latest"]
        side = {"buy": " (خرید)", "sell": " (فروش)"}.get(lt["side"] or "", "")
        row = f"{RLM}• {e((lt['asset_name'] or '') + side)}: {e(fmt_amount(lt, du, sep=sep))}"
        ch = fmt_change(it["pct_change"], it["abs_change"], lt["canonical_unit"], du, _window_label(hours * 60), sep=sep)
        row += f"  {e(ch)}" if ch else ""
        if show_src:
            row += f"  — {e(it['source_name'])}"
        lines.append(row)
    lines += ["", f"{RLM}تغییرها فقط برای دارایی‌هایی نمایش داده می‌شود که داده‌ی کافی دارند.", _brand(conn)]
    return "\n".join(lines)[:MAX_LEN]


def render_market_bulletin(conn: sqlite3.Connection, tzname: str = "Asia/Tehran", now: datetime | None = None) -> str:
    now = now or utcnow()
    sep = get_setting(conn, "thousands_separator") or ","
    dt = now.astimezone(tz(tzname))
    jy, jm, jd = to_jalali_date(dt)

    query = """
    SELECT r.*, s.name as source_name, s.attribution
    FROM records r
    JOIN sources s ON s.id = r.source_id
    WHERE r.amount_canonical IS NOT NULL AND r.amount > 0
    ORDER BY r.id DESC
    """
    rows = conn.execute(query).fetchall()
    seen = set()
    latest_items = []
    for r in rows:
        key = (r["asset_key"], r["side"])
        if key not in seen:
            seen.add(key)
            latest_items.append(r)

    lines = [
        f"{RLM}📊 <b>نبض بازار</b>",
        f"{RLM}📅 تاریخ: {to_persian_digits(f'{jy}/{jm:02d}/{jd:02d}')} — ساعت: {to_persian_digits(dt.strftime('%H:%M'))}",
        "────────────────────"
    ]

    PRIORITY = {
        # Currency
        "usd": 10, "usd_herat": 11, "eur": 12, "gbp": 13, "aed": 14, "try": 15,
        "cad": 16, "aud": 17, "cny": 18, "iqd": 19, "afn": 20, "omr": 21, "azn": 22,

        # Gold & Coins
        "gold18": 30, "gold24": 31, "gold_melted": 32, "mesghal": 33, "gold_ounce": 34,
        "coin_emami": 35, "coin_bahar": 36, "coin_half": 37, "coin_quarter": 38, "coin_gerami": 39,

        # Energy
        "oil_brent": 50,

        # Crypto
        "usdt": 60, "btc": 61, "eth": 62, "ton": 63, "trx": 64, "sol": 65, "xrp": 66, "doge": 67, "bnb": 68, "paxg": 69,
    }
    latest_items.sort(key=lambda x: (PRIORITY.get(x["asset_key"], 99), -(x["amount"] or 0)))

    currencies = []
    gold_coins = []
    energy_items = []
    cryptos = []
    cars = []

    for it in latest_items:
        cat = it["category"]
        name = it["asset_name"] or it["asset_key"]
        side_fa = " (خرید)" if it["side"] == "buy" else (" (فروش)" if it["side"] == "sell" else "")
        unit = it["stated_unit"] or "تومان"
        unit_str = "تومان" if unit == "IRT" else ("ریال" if unit == "IRR" else ("دلار" if unit == "USD" else unit))
        is_float = bool(it['amount'] and (float(it['amount']) % 1 != 0 or float(it['amount']) < 100))
        dec = 2 if is_float else 0
        amt_str = fmt_number(it['amount'], decimals=dec, sep=sep)

        # Determine percentage change and direction indicator
        chg_str = ""
        extras = jload(it["extras"], {}) if isinstance(it["extras"], str) else (it["extras"] or {})
        change_pct = extras.get("change_pct")

        if change_pct is None and it["amount_canonical"] is not None:
            prev = conn.execute(
                "SELECT amount_canonical, canonical_unit FROM records WHERE entity_key=? AND canonical_unit=? AND id<? AND amount_canonical IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (it["entity_key"], it["canonical_unit"], it["id"])
            ).fetchone()
            if prev and prev["amount_canonical"] and it["amount_canonical"]:
                prev_amt = prev["amount_canonical"]
                curr_amt = it["amount_canonical"]
                if prev_amt > 0:
                    diff_pct = ((curr_amt - prev_amt) / prev_amt) * 100
                    if 0.01 <= abs(diff_pct) < 1000:
                        change_pct = round(diff_pct, 2)

        if change_pct is not None:
            try:
                change_pct = float(change_pct)
                if change_pct > 0:
                    chg_str = f" (↑ {to_persian_digits(f'{change_pct:.2f}')}٪ 🟢)"
                elif change_pct < 0:
                    chg_str = f" (↓ {to_persian_digits(f'{abs(change_pct):.2f}')}٪ 🔴)"
                else:
                    chg_str = " (بدون تغییر ➖)"
            except (ValueError, TypeError):
                chg_str = ""

        if cat == "car":
            CAR_MODEL_FA = {
                "corolla": "کرولا", "frontlander": "فرانتلندر", "cross": "کراس",
                "pride": "پراید", "tiba": "تیبا", "saina": "ساینا", "quick": "کوییک",
                "shahin": "شاهین", "tara": "تارا", "dena": "دنا", "samand": "سمند",
                "runna": "رانا", "soren": "سورن", "re_ra": "ری‌را", "arisan": "آریسان",
                "pickup": "پیکاپ/وانت", "atlas": "اطلس", "sahand": "سهند", "c3": "سی ۳",
            }
            m_fa = CAR_MODEL_FA.get(it["model"], it["model"])
            name = " ".join(x for x in [it["asset_name"], m_fa, it["variant"]] if x)
        else:
            name = it["asset_name"] or it["asset_key"]
        entry = f"{RLM}• <b>{e(name)}{side_fa}</b>: {amt_str} {unit_str}{chg_str}"

        if cat == "currency":
            currencies.append(entry)
        elif cat in ("gold", "coin"):
            gold_coins.append(entry)
        elif cat == "energy" or it["asset_key"] == "oil_brent":
            energy_items.append(entry)
        elif cat == "crypto":
            cryptos.append(entry)
        elif cat == "car":
            if it['amount'] and it['amount'] >= 100_000_000:
                cars.append(entry)

    if currencies:
        lines.append(f"{RLM}💵 <b>ارز و اسکناس:</b>")
        lines.extend(currencies[:10])
        lines.append("")

    if gold_coins:
        lines.append(f"{RLM}🪙 <b>طلا و مسکوکات:</b>")
        lines.extend(gold_coins[:10])
        lines.append("")

    if energy_items:
        lines.append(f"{RLM}🛢️ <b>نفت و انرژی:</b>")
        lines.extend(energy_items[:2])
        lines.append("")

    if cryptos:
        lines.append(f"{RLM}🌐 <b>رمزارزها:</b>")
        lines.extend(cryptos[:8])
        lines.append("")

    if cars:
        lines.append(f"{RLM}🚗 <b>بازار و قیمت خودرو:</b>")
        lines.extend(cars[:8])
        lines.append("")

    lines.append("────────────────────")
    lines.append(_brand(conn))
    return "\n".join(lines)[:MAX_LEN]


def render_car_bulletin(conn: sqlite3.Connection, tzname: str = "Asia/Tehran", now: datetime | None = None) -> str:
    now = now or utcnow()
    sep = get_setting(conn, "thousands_separator") or ","
    dt = now.astimezone(tz(tzname))
    jy, jm, jd = to_jalali_date(dt)

    query = """
    SELECT r.*, s.name as source_name
    FROM records r
    JOIN sources s ON s.id = r.source_id
    WHERE r.category = 'car' AND r.amount_canonical IS NOT NULL AND r.amount >= 100000000
    ORDER BY r.id DESC
    """
    rows = conn.execute(query).fetchall()
    seen = set()
    latest_cars = []
    for r in rows:
        key = (r["brand"], r["model"])
        if key not in seen:
            seen.add(key)
            latest_cars.append(r)

    from .catalog import default_catalog
    catalog = default_catalog()

    domestic = []
    montazhi = []
    imported = []
    commercial = []

    for it in latest_cars:
        binfo = catalog.brands.get(it["brand"] or "", {})
        seg = binfo.get("segment", "domestic")
        brand_fa = binfo.get("name_fa") or it["asset_name"] or it["brand"] or ""
        m_fa = it["model"] or ""
        models_dict = binfo.get("models", {})
        if it["model"] in models_dict and models_dict[it["model"]]:
            m_fa = models_dict[it["model"]][0]

        clean_brand = brand_fa.replace("\u200c", "").replace(" ", "").lower()
        clean_model = m_fa.replace("\u200c", "").replace(" ", "").lower()
        name_parts = [brand_fa]
        if m_fa and clean_model not in clean_brand:
            name_parts.append(m_fa)
        if it["variant"]:
            var = str(it["variant"])
            clean_var = var.replace("\u200c", "").replace(" ", "").lower()
            if clean_var not in clean_brand and (not m_fa or clean_var not in clean_model):
                name_parts.append(var)
        name = " ".join(name_parts).strip()

        amt_str = fmt_number(it["amount"], decimals=0, sep=sep)
        entry = f"{RLM}• <b>{e(name)}</b>: {amt_str} تومان"

        if seg == "domestic":
            domestic.append(entry)
        elif seg == "montazhi":
            montazhi.append(entry)
        elif seg == "import":
            imported.append(entry)
        elif seg == "commercial":
            commercial.append(entry)
        else:
            domestic.append(entry)

    lines = [
        f"{RLM}🚗 <b>نبض بازار خودرو</b>",
        f"{RLM}📅 تاریخ: {to_persian_digits(f'{jy}/{jm:02d}/{jd:02d}')} — ساعت: {to_persian_digits(dt.strftime('%H:%M'))}",
        "────────────────────"
    ]

    if domestic:
        lines.append(f"{RLM}🇮🇷 <b>محصولات ایران‌خودرو و سایپا:</b>")
        lines.extend(domestic[:8])
        lines.append("")

    if montazhi:
        lines.append(f"{RLM}🚙 <b>خودروهای مونتاژی (چینی‌های پرطرفدار):</b>")
        lines.extend(montazhi[:8])
        lines.append("")

    if imported:
        lines.append(f"{RLM}🌐 <b>خودروهای وارداتی:</b>")
        lines.extend(imported[:8])
        lines.append("")

    if commercial:
        lines.append(f"{RLM}🚚 <b>وانت، پیکاپ و خودروهای تجاری / سنگین:</b>")
        lines.extend(commercial[:8])
        lines.append("")

    if not (domestic or montazhi or imported or commercial):
        lines.append(f"{RLM}اطلاعات جدیدی از قیمت خودرو در پایگاه داده ثبت نشده است.")
        lines.append("")

    lines.append("────────────────────")
    lines.append(f"{RLM}#نبض_خودرو #قیمت_روز #بازار_خودرو")
    lines.append(_brand(conn))
    return "\n".join(lines)[:MAX_LEN]



def render_opportunity(conn: sqlite3.Connection, opp: Any, tzname: str) -> tuple[str, list[list[dict[str, str]]]]:
    ev = jload(opp["evidence"], {})
    sep = get_setting(conn, "thousands_separator") or ","
    show_src = int(get_setting(conn, "show_source") if get_setting(conn, "show_source") is not None else 1)
    show_url = int(get_setting(conn, "show_source_url") if get_setting(conn, "show_source_url") is not None else 1)
    lines = [f"{RLM}💡 <b>{e(opp['title'])}</b>", "", f"{RLM}{e(opp['explanation'])}", ""]
    for o in ev.get("observations", [])[:4]:
        src_part = f" — {e(o['source'])}" if show_src else ""
        lines.append(f"{RLM}• {e(o['label'])}: {e(o['value'])}{src_part} — {e(fmt_time(o['observed_at'], tzname))}")
    lines += ["", f"{RLM}⚠️ {e(opp['assumptions'])}",
              f"{RLM}امتیاز تحلیلی (غیرکالیبره، نه احتمال): {fmt_number(opp['score'], sep=sep)} از ۱۰۰",
              f"{RLM}این پیام صرفاً اطلاع‌رسانی است و توصیه‌ی سرمایه‌گذاری یا خرید نیست.", "", _brand(conn)]
    buttons = []
    if show_url:
        for o in ev.get("observations", [])[:2]:
            if o.get("url"):
                buttons.append({"text": f"منبع: {o['source']}"[:40], "url": o["url"]})
    return "\n".join(lines)[:MAX_LEN], ([buttons] if buttons else [])


# ---- outbox ---------------------------------------------------------------------------
def initial_status(conn: sqlite3.Connection, kind: str, category: str, confidence: float | None,
                   approved: bool = False) -> str:
    if approved:
        return "queued"
    mode = get_setting(conn, "publish_mode")
    if mode == "manual":
        return "pending_approval"
    if kind in ("bulletin", "summary") and mode in ("auto", "auto_all", "auto_categories"):
        return "queued"
    if kind == "opportunity":
        if get_setting(conn, "auto_approve_opportunities") == 1 or mode == "auto_all":
            return "queued"
        return "pending_approval"
    conf_ok = confidence is not None and confidence >= float(get_setting(conn, "min_auto_confidence"))
    if mode in ("auto", "auto_all") and conf_ok:
        return "queued"
    if mode == "auto_categories" and conf_ok and category in get_setting(conn, "auto_categories"):
        return "queued"
    return "pending_approval"


def enqueue(conn: sqlite3.Connection, *, kind: str, category: str, text: str,
            buttons: list[list[dict[str, str]]], key: str, record_ids: list[int], confidence: float | None,
            now: datetime, ref: tuple[str, int] | None = None, approved_by: str | None = None) -> int | None:
    status = initial_status(conn, kind, category, confidence, approved=approved_by is not None)
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox(kind,category,ref_type,ref_id,text,buttons,idempotency_key,record_ids,"
        "confidence,status,created_at,approved_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, category, ref[0] if ref else None, ref[1] if ref else None, text, jdump(buttons), key,
         jdump(record_ids), confidence, status, iso(now), approved_by))
    return int(cur.lastrowid) if cur.rowcount else None


def maybe_enqueue_alert(conn: sqlite3.Connection, rec: Any, now: datetime, tzname: str) -> int | None:
    if rec["category"] not in ("currency", "gold", "coin", "crypto"):
        return None
    window = int(get_setting(conn, "alert_window_minutes"))
    ch = analytics.change_over(conn, rec, window)
    if ch is None or abs(ch.pct_change) < float(get_setting(conn, "alert_change_pct")):
        if get_setting(conn, "auto_publish_quotes") == 1:
            rep_win = int(get_setting(conn, "repeat_window_minutes") or 30)
            recent = conn.execute(
                "SELECT id FROM outbox WHERE idempotency_key LIKE ? AND created_at > ? AND status != 'cancelled'",
                (f"%:{rec['entity_key']}:%", iso(now - timedelta(minutes=rep_win)))
            ).fetchone()
            if recent is None:
                corr = analytics.corroborating(conn, rec, now)
                text, buttons = render_record(conn, rec, tzname, ch, len(corr), "📊 <b>اعلام قیمت بازار</b>")
                hour_slot = now.strftime("%Y%m%d%H%M")[:11]
                return enqueue(conn, kind="quote", category=rec["category"], text=text, buttons=buttons,
                               key=f"quote:{rec['entity_key']}:{hour_slot}", record_ids=[rec["id"]],
                               confidence=rec["confidence"], now=now, ref=("record", rec["id"]))
        return None
    corr = analytics.corroborating(conn, rec, now)
    text, buttons = render_record(conn, rec, tzname, ch, len(corr), "🚨 <b>تغییر قابل‌توجه قیمت</b>")
    return enqueue(conn, kind="alert", category=rec["category"], text=text, buttons=buttons,
                   key=f"alert:{rec['id']}", record_ids=[rec["id"]], confidence=rec["confidence"], now=now,
                   ref=("record", rec["id"]))


def schedule_summaries(conn: sqlite3.Connection, now: datetime, tzname: str) -> list[int]:
    local = now.astimezone(tz(tzname))
    if local.hour not in get_setting(conn, "summary_hours_local"):
        return []
    ids = []
    for c in conn.execute("SELECT key FROM categories WHERE enabled=1 AND key NOT IN ('car','deal')"):
        items = analytics.daily_summary(conn, c["key"], now)
        text = render_summary(conn, c["key"], items, tzname, now)
        if text:
            rids = [i["latest"]["id"] for i in items]
            confs = [i["latest"]["confidence"] for i in items]
            oid = enqueue(conn, kind="summary", category=c["key"], text=text, buttons=[],
                          key=f"summary:{c['key']}:{local:%Y-%m-%d}:{local.hour}", record_ids=rids,
                          confidence=min(confs) if confs else None, now=now)
            if oid:
                ids.append(oid)
    return ids


def schedule_bulletin(conn: sqlite3.Connection, now: datetime, tzname: str) -> int | None:
    interval_m = int(get_setting(conn, "auto_bulletin_interval_minutes") or 0)
    if interval_m <= 0:
        return None
    mode = get_setting(conn, "publish_mode")
    if mode not in ("auto", "auto_all", "auto_categories"):
        return None
    cutoff = iso(now - timedelta(minutes=interval_m))
    recent = conn.execute(
        "SELECT id FROM outbox WHERE kind='bulletin' AND created_at > ? AND status != 'cancelled'",
        (cutoff,)
    ).fetchone()
    if recent is not None:
        return None
    text = render_market_bulletin(conn, tzname, now)
    if not text:
        return None
    slot = now.strftime("%Y%m%d%H%M")
    return enqueue(conn, kind="bulletin", category="all", text=text, buttons=[],
                   key=f"bulletin:{slot}", record_ids=[], confidence=1.0, now=now)


def approve(conn: sqlite3.Connection, outbox_id: int, actor: str) -> bool:
    cur = conn.execute("UPDATE outbox SET status='queued', approved_by=? WHERE id=? AND status IN "
                       "('pending_approval','failed','uncertain')", (actor, outbox_id))
    if cur.rowcount:
        audit(conn, actor, "outbox_approve", {"id": outbox_id})
    return bool(cur.rowcount)


def cancel(conn: sqlite3.Connection, outbox_id: int, actor: str) -> bool:
    cur = conn.execute("UPDATE outbox SET status='cancelled' WHERE id=? AND status IN "
                       "('pending_approval','queued','failed','uncertain')", (outbox_id,))
    if cur.rowcount:
        audit(conn, actor, "outbox_cancel", {"id": outbox_id})
    return bool(cur.rowcount)


def retry(conn: sqlite3.Connection, outbox_id: int, actor: str, confirm_uncertain: bool = False) -> bool:
    """Manual retry. 'uncertain' items may already be on the channel, so they need explicit confirmation."""
    allowed = "('failed','uncertain')" if confirm_uncertain else "('failed')"
    cur = conn.execute(f"UPDATE outbox SET status='queued', next_attempt_at=NULL, attempts=0, last_error=NULL "
                       f"WHERE id=? AND status IN {allowed}", (outbox_id,))
    if cur.rowcount:
        audit(conn, actor, "outbox_retry", {"id": outbox_id, "uncertain": confirm_uncertain})
    return bool(cur.rowcount)


def cancel_for_records(conn: sqlite3.Connection, record_ids: list[int], reason: str) -> int:
    n = 0
    ids = set(record_ids)
    for r in conn.execute("SELECT id, record_ids FROM outbox WHERE status IN ('pending_approval','queued')").fetchall():
        if ids & set(json.loads(r["record_ids"])):
            conn.execute("UPDATE outbox SET status='cancelled', last_error=? WHERE id=?", (reason, r["id"]))
            n += 1
    return n


def in_quiet_hours(conn: sqlite3.Connection, now: datetime, tzname: str) -> bool:
    qh = get_setting(conn, "quiet_hours")
    if not qh:
        return False
    a, b = qh.split("-")
    cur = now.astimezone(tz(tzname)).strftime("%H:%M")
    return (a <= cur < b) if a <= b else (cur >= a or cur < b)


def dispatch_due(conn: sqlite3.Connection, sender: Sender, chat_id: str, now: datetime, tzname: str,
                 max_items: int = 10) -> dict[str, int]:
    stats = {"sent": 0, "retry": 0, "failed": 0, "uncertain": 0, "skipped": 0}
    # a crash between "sending" and the result leaves a lease behind -> never auto-resend (could duplicate)
    for r in conn.execute("SELECT id FROM outbox WHERE status='sending' AND lease_until<?", (iso(now),)).fetchall():
        conn.execute("UPDATE outbox SET status='uncertain', last_error='crashed mid-send; check channel then retry' "
                     "WHERE id=?", (r["id"],))
        log_event(conn, "ERROR", "publisher", "outbox item left in 'sending'; marked uncertain", outbox_id=r["id"])
        stats["uncertain"] += 1
    if get_setting(conn, "paused"):
        stats["skipped"] = -1
        return stats
    if in_quiet_hours(conn, now, tzname):
        return stats
    interval = float(get_setting(conn, "min_interval_seconds"))
    for _ in range(max_items):
        last = conn.execute("SELECT MAX(sent_at) m FROM outbox").fetchone()["m"]
        if last and interval and (now - parse_iso(last)).total_seconds() < interval:
            break
        row = conn.execute("SELECT * FROM outbox WHERE status='queued' AND (next_attempt_at IS NULL OR "
                           "next_attempt_at<=?) ORDER BY id LIMIT 1", (iso(now),)).fetchone()
        if row is None:
            break
        with tx(conn):
            claimed = conn.execute(
                "UPDATE outbox SET status='sending', attempts=attempts+1, lease_until=?, target_chat=? "
                "WHERE id=? AND status='queued'",
                (iso(now + timedelta(seconds=int(get_setting(conn, "lease_seconds")))), chat_id, row["id"])).rowcount
        if not claimed:
            continue
        try:
            mid = sender.send(chat_id, row["text"], json.loads(row["buttons"]))
        except RateLimited as ex:
            conn.execute("UPDATE outbox SET status='queued', next_attempt_at=?, last_error=?, lease_until=NULL "
                         "WHERE id=?", (iso(now + timedelta(seconds=ex.retry_after + 1)), str(ex), row["id"]))
            stats["retry"] += 1
            break  # honour Telegram's flood control for everything
        except PermanentError as ex:
            conn.execute("UPDATE outbox SET status='failed', last_error=?, lease_until=NULL WHERE id=?",
                         (str(ex)[:300], row["id"]))
            log_event(conn, "ERROR", "publisher", f"permanent send failure: {ex}", outbox_id=row["id"])
            stats["failed"] += 1
        except SendError as ex:
            attempts = row["attempts"] + 1
            if attempts >= int(get_setting(conn, "max_attempts")):
                conn.execute("UPDATE outbox SET status='failed', last_error=?, lease_until=NULL WHERE id=?",
                             (str(ex)[:300], row["id"]))
                stats["failed"] += 1
            else:
                delay = min(5 * 2 ** attempts, 900)
                conn.execute("UPDATE outbox SET status='queued', next_attempt_at=?, last_error=?, lease_until=NULL "
                             "WHERE id=?", (iso(now + timedelta(seconds=delay)), str(ex)[:300], row["id"]))
                stats["retry"] += 1
        else:
            conn.execute("UPDATE outbox SET status='sent', telegram_message_id=?, sent_at=?, lease_until=NULL, "
                         "last_error=NULL WHERE id=?", (mid, iso(now), row["id"]))
            if row["ref_type"] == "opportunity":
                conn.execute("UPDATE opportunities SET status='published' WHERE id=?", (row["ref_id"],))
            stats["sent"] += 1
    return stats
