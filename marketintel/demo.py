"""Synthetic demonstration data. ALL VALUES ARE FAKE and are never real market prices."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import sqlite3

from .catalog import default_catalog
from .sources import load_categories, upsert_source

A_CHAT, B_CHAT = -1001000000001, -1001000000002


def seed_sources(conn: sqlite3.Connection) -> None:
    load_categories(conn, default_catalog().categories)
    upsert_source(conn, "demo_a", name="کانال آزمایشی الف", chat_ref=str(A_CHAT), reliability=0.85,
                  categories=["currency", "gold", "coin", "car", "deal"], attribution="کانال آزمایشی الف (داده‌ی ساختگی)",
                  url_template="https://example.invalid/demo_a/{message_id}", restrictions="demo only")
    upsert_source(conn, "demo_b", name="کانال آزمایشی ب", chat_ref=str(B_CHAT), reliability=0.7, default_unit="IRT",
                  categories=["currency", "gold", "coin", "car", "deal"], attribution="کانال آزمایشی ب (داده‌ی ساختگی)",
                  url_template="https://example.invalid/demo_b/{message_id}")


def _upd(uid: int, chat: int, mid: int, when: datetime, text: str, edited: bool = False) -> dict[str, Any]:
    key = "edited_channel_post" if edited else "channel_post"
    return {"update_id": uid, key: {"message_id": mid, "date": int(when.timestamp()),
                                    "chat": {"id": chat, "type": "channel"}, "text": text}}


def demo_updates(now: datetime) -> list[dict[str, Any]]:
    m = lambda x: now - timedelta(minutes=x)  # noqa: E731
    gold_list = "طلای ۱۸ عیار ۳,۰۰۰,۰۰۰ تومان\nسکه امامی ۴۰,۰۰۰,۰۰۰ تومان\nنیم سکه ۲۲,۰۰۰,۰۰۰ تومان\nربع سکه ۱۳,۰۰۰,۰۰۰ تومان"
    return [
        _upd(1, A_CHAT, 1, m(120), "دلار: ۵۰٬۰۰۰ تومان (آزمایشی)"),
        _upd(2, A_CHAT, 2, m(100), "یورو ۵۵٬۰۰۰ تومان (آزمایشی)"),
        _upd(3, A_CHAT, 3, m(60), "دلار: ۵۰٬۳۰۰ تومان (آزمایشی)"),
        _upd(4, A_CHAT, 4, m(55), "قیمت‌ها به تومان (آزمایشی)\n" + gold_list),
        _upd(5, B_CHAT, 1, m(50), "قیمت‌ها به تومان (آزمایشی)\n" + gold_list),          # syndicated copy
        _upd(6, A_CHAT, 5, m(20), "یورو ۵٬۵۰۰ تومان (آزمایشی)"),                        # scale error
        _upd(7, A_CHAT, 6, m(15), "سکه بهار آزادی ۴۲۰۰۰۰۰۰ (آزمایشی)"),                 # missing unit
        _upd(8, A_CHAT, 7, m(6), "دلار: ۵۱٬۵۰۰ تومان (آزمایشی)"),                        # +2.4% alert
        _upd(9, B_CHAT, 2, m(4), "دلار ۵۰٬۱۰۰ (آزمایشی)"),                                # unit by source default
        _upd(10, A_CHAT, 8, m(30), "پژو ۲۰۶ تیپ ۵ مدل ۱۴۰۲ تهران ۴۰۰ میلیون تومان (آزمایشی)"),
        _upd(11, A_CHAT, 9, m(29), "پژو ۲۰۶ تیپ ۵ مدل ۱۴۰۲ تهران ۴۰۲ میلیون تومان (آزمایشی)"),
        _upd(12, A_CHAT, 10, m(28), "پژو ۲۰۶ تیپ ۵ مدل ۱۴۰۲ تهران ۳۹۸ میلیون تومان (آزمایشی)"),
        _upd(13, A_CHAT, 11, m(27), "پژو ۲۰۶ تیپ ۵ مدل ۱۴۰۲ تهران ۳۰۰ میلیون تومان (آزمایشی)"),
        _upd(14, A_CHAT, 12, m(26), "پژو ۲۰۶ تیپ ۲ مدل ۱۴۰۲ تهران ۲۸۰ میلیون تومان (آزمایشی)"),  # different variant
        _upd(15, A_CHAT, 13, m(10), "هدفون آزمایشی تخفیف ۴۰٪ قیمت قبلی ۵,۰۰۰,۰۰۰ تومان قیمت جدید ۳,۰۰۰,۰۰۰ تومان کد: DEMO40 تا ۲۴ ساعت دیگر"),
        _upd(16, A_CHAT, 7, m(2), "دلار: ۵۱٬۶۰۰ تومان (آزمایشی - ویرایش‌شده)", edited=True),
        _upd(17, A_CHAT, 14, m(1), "امروز بازار آرام بود"),                                 # unparsed
    ]
