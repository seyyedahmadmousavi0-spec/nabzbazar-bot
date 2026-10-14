"""Automotive announcements, pre-order alerts, factory price changes, and profit margins."""
from __future__ import annotations

import html
import re
import sqlite3
from typing import Any

from .textnorm import normalize_for_match, to_persian_digits

RLM = "\u200f"

# Filter out common channel ads and bot promotions
AD_PATTERNS = [
    r"🤖\s*دریافت اشتراک.*",
    r"🎨\s*هوش مصنوعی.*",
    r"📸\s*ساخت عکس.*",
    r"⭕️\s*چت جی پی تی.*",
    r"@[a-zA-Z0-9_]{4,}",
    r"https?://t\.me/[a-zA-Z0-9_]+",
]


def clean_text(text: str) -> str:
    cleaned = text
    for pat in AD_PATTERNS:
        cleaned = re.sub(pat, "", cleaned, flags=re.I)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def detect_car_announcement(raw_text: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    norm = normalize_for_match(raw_text)
    if not norm:
        return None

    # Ignore pure price lists (those have many ⬅️ or many bullet prices)
    if norm.count("⬅️") >= 3 or norm.count("قیمت های اعلامی") >= 1:
        return None

    # Check for Factory Price Change & Profit Margin
    if any(k in norm for k in ("قیمت جدید", "نرخ جدید", "میزان افزایش", "قیمت قبل", "افزایش قیمت")):
        lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
        new_price_m = re.search(r"قیمت\s*(?:جدید|مصوب)?\s*[:：]\s*([\d,]+(?:\.\d+)?)\s*(تومان|ریال)?", raw_text)
        old_price_m = re.search(r"قیمت\s*(?:قبل|قبلی)?\s*[:：]\s*([\d,]+(?:\.\d+)?)\s*(تومان|ریال)?", raw_text)
        inc_m = re.search(r"(?:میزان\s*)?افزایش\s*[:：]\s*([\d,]+(?:\.\d+)?)\s*(تومان|ریال)?", raw_text)

        if new_price_m:
            title_line = lines[0] if lines else "اعلام قیمت جدید کارخانه"
            title_line = re.sub(r"^[🔴🟢⚪️🔵🔹▪️\s\-]+", "", title_line)
            car_name = title_line
            for prefix in ("قیمت جدید", "افزایش قیمت", "شرکت"):
                if prefix in car_name:
                    car_name = car_name.replace(prefix, "").strip(" :؛-")

            new_val = new_price_m.group(1).replace(",", "")
            old_val = old_price_m.group(1).replace(",", "") if old_price_m else None
            inc_val = inc_m.group(1).replace(",", "") if inc_m else None

            # Calculate profit margin vs market price if database connection is available
            market_price = None
            profit_margin = None
            if conn:
                # search for recent market price of similar vehicle
                row = conn.execute(
                    "SELECT amount, stated_unit FROM records WHERE category='car' AND (asset_name LIKE ? OR snippet LIKE ?) "
                    "AND amount > 0 ORDER BY id DESC LIMIT 1",
                    (f"%{car_name[:10]}%", f"%{car_name[:10]}%")
                ).fetchone()
                if row:
                    amt = row["amount"] if hasattr(row, "keys") else row[0]
                    if amt:
                        market_price = float(amt)
                    try:
                        n_val = float(new_val)
                        if market_price > n_val:
                            profit_margin = market_price - n_val
                    except ValueError:
                        pass

            return {
                "kind": "factory_price_change",
                "title": title_line,
                "car_name": car_name or title_line,
                "new_price": new_price_m.group(0),
                "old_price": old_price_m.group(0) if old_price_m else None,
                "increase": inc_m.group(0) if inc_m else None,
                "market_price": market_price,
                "profit_margin": profit_margin,
                "raw_cleaned": clean_text(raw_text)
            }

    # Check for Pre-order & Sales Conditions (ثبت نام، پیش‌فروش، حساب وکالتی)
    if any(k in norm for k in ("حساب وکالتی", "وکالتی کردن", "پیش فروش", "فروش فوق العاده", "پیش ثبت", "سامانه یکپارچه", "ثبت سفارش")):
        title_line = ""
        for line in raw_text.splitlines():
            line_str = line.strip()
            if line_str and not line_str.startswith("#"):
                title_line = re.sub(r"^[🔴🟢⚪️🔵🔹▪️\s\-]+", "", line_str)
                break

        deposit_m = re.search(r"مبلغ\s*حساب\s*وکالتی\s*[:：]?\s*([^\n\r]+)", raw_text)
        deadline_m = re.search(r"مهلت\s*(?:وکالتی|وکالتی‌کردن|حساب)\s*[:：]?\s*([^\n\r]+)", raw_text)
        order_m = re.search(r"مهلت\s*ثبت\s*(?:سفارش|نام)?\s*[:：]?\s*([^\n\r]+)", raw_text)
        release_m = re.search(r"زمان\s*آزادسازی\s*(?:وجوه|مبالغ)?\s*[:：]?\s*([^\n\r]+)", raw_text)

        return {
            "kind": "pre_order",
            "title": title_line or "اطلاعیه شرایط فروش و ثبت‌نام خودرو",
            "deposit": deposit_m.group(1).strip() if deposit_m else None,
            "bank_deadline": deadline_m.group(1).strip() if deadline_m else None,
            "order_deadline": order_m.group(1).strip() if order_m else None,
            "release_date": release_m.group(1).strip() if release_m else None,
            "is_lottery": "قرعه کشی" in norm,
            "raw_cleaned": clean_text(raw_text)
        }

    # Check for Directives & Policy News (بخشنامه، تغییر رنگ، منطقه آزاد، تعرفه واردات)
    if any(k in norm for k in ("بخشنامه", "تغییر رنگ", "منطقه آزاد", "تعرفه واردات", "شورای رقابت", "سازمان حمایت", "تبدیل تعهدات", "دعوتنامه")):
        lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
        title_line = lines[0] if lines else "بخشنامه و تحولات بازار خودرو"
        title_line = re.sub(r"^[🔴🟢⚪️🔵🔹▪️\s\-]+", "", title_line)
        return {
            "kind": "directive",
            "title": title_line,
            "raw_cleaned": clean_text(raw_text)
        }

    return None


def render_car_announcement(ann: dict[str, Any], brand_line: str = "@nabzbazar360") -> str:
    kind = ann["kind"]
    lines = []

    if kind == "factory_price_change":
        lines.append(f"{RLM}🏭 <b>اعلام قیمت جدید کارخانه و حاشیه سود</b>")
        lines.append("────────────────────")
        lines.append(f"{RLM}🚗 <b>{html.escape(ann['car_name'])}</b>")
        lines.append("")
        if ann.get("new_price"):
            lines.append(f"{RLM}• {to_persian_digits(ann['new_price'])}")
        if ann.get("old_price"):
            lines.append(f"{RLM}• {to_persian_digits(ann['old_price'])}")
        if ann.get("increase"):
            lines.append(f"{RLM}• 🔺 {to_persian_digits(ann['increase'])}")
        if ann.get("market_price"):
            mp_str = f"{int(ann['market_price']):,}"
            lines.append(f"{RLM}• 💰 قیمت بازار آزاد: {to_persian_digits(mp_str)} تومان")
        if ann.get("profit_margin") and ann["profit_margin"] > 0:
            pm_str = f"{int(ann['profit_margin']):,}"
            lines.append(f"{RLM}• 📈 <b>سود ثبت‌نام (حاشیه سود کارخانه تا بازار): {to_persian_digits(pm_str)} تومان</b>")
        lines.append("")
        lines.append("────────────────────")
        lines.append(f"{RLM}#قیمت_کارخانه #سود_ثبت_نام #نبض_بازار")

    elif kind == "pre_order":
        lines.append(f"{RLM}🚨 <b>اطلاعیه رسمی ثبت‌نام و شرایط فروش خودرو</b>")
        lines.append("────────────────────")
        lines.append(f"{RLM}📌 <b>{html.escape(ann['title'])}</b>")
        lines.append("")
        if ann.get("deposit"):
            lines.append(f"{RLM}💳 <b>مبلغ حساب وکالتی:</b> {to_persian_digits(ann['deposit'])}")
        if ann.get("bank_deadline"):
            lines.append(f"{RLM}⏳ <b>مهلت وکالتی کردن حساب:</b> {to_persian_digits(ann['bank_deadline'])}")
        if ann.get("order_deadline"):
            lines.append(f"{RLM}📅 <b>مهلت ثبت سفارش:</b> {to_persian_digits(ann['order_deadline'])}")
        if ann.get("release_date"):
            lines.append(f"{RLM}🔓 <b>آزادسازی مبالغ:</b> {to_persian_digits(ann['release_date'])}")
        if ann.get("is_lottery"):
            lines.append(f"{RLM}🎲 <b>نحوه عرضه:</b> قرعه‌کشی")
        lines.append("")
        lines.append("────────────────────")
        lines.append(f"{RLM}#شرایط_فروش #پیش_فروش #حساب_وکالتی #نبض_بازار")

    else:  # directive / news
        lines.append(f"{RLM}📢 <b>بخشنامه و خبر مهم بازار خودرو</b>")
        lines.append("────────────────────")
        cleaned = ann.get("raw_cleaned", "")
        # take up to first 800 characters
        snippet = cleaned[:800]
        lines.append(f"{RLM}{html.escape(snippet)}")
        lines.append("")
        lines.append("────────────────────")
        lines.append(f"{RLM}#بخشنامه_خودرو #خبر_فوری #نبض_بازار")

    if brand_line:
        lines.append(f"{RLM}{brand_line}")

    return "\n".join(lines)
