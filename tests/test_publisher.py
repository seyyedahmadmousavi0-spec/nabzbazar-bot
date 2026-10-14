import json
import unittest
from datetime import timedelta

from marketintel import db, opportunities, publisher
from marketintel.senders import MockSender, PermanentError, RateLimited, TransientError
from marketintel.util import iso

from .helpers import NOW, add_source, make_db, post, rec


class Formatting(unittest.TestCase):
    def setUp(self):
        self.c = make_db()
        self.a = add_source(self.c, "a", attribution="کانال <الف>")

    def test_record_post_persian_rtl_no_placeholders_and_escaped(self):
        (r,) = post(self.c, self.a, "1", "دلار: ۵۰٬۰۰۰ تومان", 5)
        text, buttons = publisher.render_record(self.c, rec(self.c, r), "Asia/Tehran")
        self.assertIn("۵۰٬۰۰۰ تومان", text)
        self.assertIn("\u200f• دارایی: دلار آمریکا", text)
        self.assertIn("&lt;الف&gt;", text)          # untrusted text escaped for HTML parse mode
        self.assertNotIn("[", text.replace("[]", ""))   # no template placeholders
        self.assertIn("زمان مشاهده", text)
        self.assertNotIn("تغییر", text)             # no change line without a valid comparison
        self.assertIn("تأییدنشده", text)            # single source is never called verified
        self.assertEqual(buttons[0][0]["url"], "https://t.me/a/1")
        self.assertLessEqual(len(text), 4096)

    def test_change_line_only_with_valid_base(self):
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 60)
        (r,) = post(self.c, self.a, "2", "دلار ۵۱٬۰۰۰ تومان", 0)
        from marketintel import analytics
        ch = analytics.change_over(self.c, rec(self.c, r), 60)
        text, _ = publisher.render_record(self.c, rec(self.c, r), "Asia/Tehran", ch)
        self.assertIn("🔺 +۱٬۰۰۰", text)
        self.assertIn("+۲٫۰۰٪", text)

    def test_corroboration_wording(self):
        self.assertIn("مستقل", publisher.verification_line(2))
        self.assertIn("تأییدنشده", publisher.verification_line(0))

    def test_clean_format_hides_sources_and_verification_and_custom_separator(self):
        db.set_setting(self.c, "show_source", 0)
        db.set_setting(self.c, "show_verification", 0)
        db.set_setting(self.c, "show_source_url", 0)
        db.set_setting(self.c, "thousands_separator", ",")
        (r,) = post(self.c, self.a, "1", "دلار: ۵۰٬۰۰۰ تومان", 5)
        text, buttons = publisher.render_record(self.c, rec(self.c, r), "Asia/Tehran")
        self.assertIn("۵۰,۰۰۰ تومان", text)
        self.assertNotIn("منبع", text)
        self.assertNotIn("&lt;الف&gt;", text)
        self.assertNotIn("وضعیت", text)
        self.assertNotIn("تأییدنشده", text)
        self.assertEqual(buttons, [])

    def test_jalali_time_in_tehran(self):
        self.assertEqual(publisher.fmt_time("2026-10-12T08:58:00Z", "Asia/Tehran"), "۱۴۰۵/۰۷/۲۰ - ۱۲:۲۸")

    def test_car_and_deal_templates(self):
        (c,) = post(self.c, self.a, "1", "پژو ۲۰۶ تیپ ۵ مدل ۱۴۰۲ تهران ۴۸۰ میلیون تومان")
        t, _ = publisher.render_record(self.c, rec(self.c, c), "Asia/Tehran")
        self.assertIn("قیمت آگهی (قیمت درخواستی)", t)
        (d,) = post(self.c, self.a, "2", "هدفون تخفیف ۴۰٪ قیمت قبلی ۵٬۰۰۰٬۰۰۰ تومان قیمت جدید ۳٬۰۰۰٬۰۰۰ تومان کد: X40")
        t, _ = publisher.render_record(self.c, rec(self.c, d), "Asia/Tehran")
        self.assertIn("اعلام‌شده توسط فروشنده", t)
        self.assertIn("<code>X40</code>", t)

    def test_summary_is_none_without_data(self):
        self.assertIsNone(publisher.render_summary(self.c, "currency", [], "Asia/Tehran", NOW))

    def test_summary_without_history_shows_no_change(self):
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 5)
        from marketintel import analytics
        t = publisher.render_summary(self.c, "currency", analytics.daily_summary(self.c, "currency", NOW), "Asia/Tehran", NOW)
        self.assertIn("دلار آمریکا", t)
        self.assertNotIn("🔺", t)

    def test_render_market_bulletin(self):
        post(self.c, self.a, "1", "دلار: ۵۰٬۰۰۰ تومان", 5)
        b = publisher.render_market_bulletin(self.c, "Asia/Tehran", NOW)
        self.assertIn("نبض بازار", b)
        first_line = b.splitlines()[0]
        self.assertIn("نبض بازار", first_line)
        self.assertNotIn("۳۶۰", first_line)
        self.assertNotIn("360", first_line)
        self.assertIn("دلار آمریکا", b)


class OutboxModes(unittest.TestCase):
    def setUp(self):
        self.c = make_db()
        self.a = add_source(self.c, "a")

    def alert_status(self):
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 60)
        post(self.c, self.a, "2", "دلار ۵۲٬۰۰۰ تومان", 0)
        return [r[0] for r in self.c.execute("SELECT status FROM outbox WHERE kind='alert'")]

    def test_manual_mode_requires_approval(self):
        self.assertEqual(self.alert_status(), ["pending_approval"])

    def test_auto_mode_queues_high_confidence_only(self):
        db.set_setting(self.c, "publish_mode", "auto")
        self.assertEqual(self.alert_status(), ["queued"])

    def test_auto_mode_low_confidence_needs_approval(self):
        db.set_setting(self.c, "publish_mode", "auto")
        db.set_setting(self.c, "min_auto_confidence", 0.99)
        self.assertEqual(self.alert_status(), ["pending_approval"])

    def test_auto_selected_categories(self):
        db.set_setting(self.c, "publish_mode", "auto_categories")
        db.set_setting(self.c, "auto_categories", ["gold"])
        self.assertEqual(self.alert_status(), ["pending_approval"])
        db.set_setting(self.c, "auto_categories", ["currency"])
        post(self.c, self.a, "3", "دلار ۵۵٬۰۰۰ تومان", 0)
        self.assertEqual([r[0] for r in self.c.execute("SELECT status FROM outbox ORDER BY id")][-1], "queued")

    def test_emergency_pause_blocks_all_sends(self):
        db.set_setting(self.c, "publish_mode", "auto")
        self.alert_status()
        db.set_setting(self.c, "paused", 1)
        s = MockSender()
        publisher.dispatch_due(self.c, s, "@dest", NOW, "Asia/Tehran")
        self.assertEqual(s.calls, [])
        db.set_setting(self.c, "paused", 0)
        publisher.dispatch_due(self.c, s, "@dest", NOW, "Asia/Tehran")
        self.assertEqual(len(s.calls), 1)

    def test_opportunities_always_need_approval_then_publish(self):
        db.set_setting(self.c, "publish_mode", "auto")
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 60)
        post(self.c, self.a, "2", "دلار ۴۷٬۰۰۰ تومان", 0)  # -6% -> price_drop
        o = self.c.execute("SELECT * FROM opportunities WHERE kind='price_drop'").fetchone()
        self.assertEqual(o["status"], "pending")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM outbox WHERE kind='opportunity'").fetchone()[0], 0)
        from marketintel import pipeline
        pipeline.decide_opportunity(self.c, o["id"], True, "ed", "Asia/Tehran", NOW)
        ob = self.c.execute("SELECT * FROM outbox WHERE kind='opportunity'").fetchone()
        self.assertEqual(ob["status"], "queued")
        self.assertIn("توصیه‌ی سرمایه‌گذاری", ob["text"])
        s = MockSender()
        publisher.dispatch_due(self.c, s, "@dest", NOW, "Asia/Tehran")
        self.assertEqual(self.c.execute("SELECT status FROM opportunities WHERE id=?", (o["id"],)).fetchone()[0], "published")

    def test_idempotent_enqueue_prevents_duplicate_publication(self):
        kw = dict(kind="summary", category="currency", text="x", buttons=[], key="k1", record_ids=[], confidence=1, now=NOW)
        self.assertIsNotNone(publisher.enqueue(self.c, **kw))
        self.assertIsNone(publisher.enqueue(self.c, **kw))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_scheduled_summary_once_per_slot(self):
        db.set_setting(self.c, "summary_hours_local", [12])   # NOW is 12:30 Tehran
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 5)
        a = publisher.schedule_summaries(self.c, NOW, "Asia/Tehran")
        b = publisher.schedule_summaries(self.c, NOW + timedelta(minutes=5), "Asia/Tehran")
        self.assertEqual((len(a), len(b)), (1, 0))
        self.assertEqual(publisher.schedule_summaries(self.c, NOW - timedelta(hours=3), "Asia/Tehran"), [])


class Delivery(unittest.TestCase):
    def setUp(self):
        self.c = make_db()
        publisher.enqueue(self.c, kind="alert", category="currency", text="hello", buttons=[], key="a1",
                          record_ids=[], confidence=1, now=NOW, approved_by="ed")

    def row(self):
        return self.c.execute("SELECT * FROM outbox").fetchone()

    def test_success_records_telegram_id_and_never_resends(self):
        s = MockSender()
        publisher.dispatch_due(self.c, s, "@d", NOW, "Asia/Tehran")
        publisher.dispatch_due(self.c, s, "@d", NOW + timedelta(minutes=1), "Asia/Tehran")
        self.assertEqual((len(s.calls), self.row()["status"], self.row()["telegram_message_id"]), (1, "sent", "100"))

    def test_rate_limit_honours_retry_after(self):
        s = MockSender([RateLimited(30), None])
        publisher.dispatch_due(self.c, s, "@d", NOW, "Asia/Tehran")
        self.assertEqual((self.row()["status"], self.row()["next_attempt_at"]), ("queued", iso(NOW + timedelta(seconds=31))))
        publisher.dispatch_due(self.c, s, "@d", NOW + timedelta(seconds=10), "Asia/Tehran")
        self.assertEqual(len(s.calls), 1)                       # not before retry_after
        publisher.dispatch_due(self.c, s, "@d", NOW + timedelta(seconds=40), "Asia/Tehran")
        self.assertEqual((len(s.calls), self.row()["status"]), (2, "sent"))

    def test_transient_error_backoff_then_success(self):
        s = MockSender([TransientError("boom"), TransientError("boom"), None])
        t = NOW
        for _ in range(3):
            publisher.dispatch_due(self.c, s, "@d", t, "Asia/Tehran")
            t += timedelta(minutes=30)
        self.assertEqual((self.row()["status"], len(s.calls)), ("sent", 3))

    def test_gives_up_after_max_attempts_and_is_visible(self):
        db.set_setting(self.c, "max_attempts", 2)
        s = MockSender([TransientError("x")] * 5)
        t = NOW
        for _ in range(4):
            publisher.dispatch_due(self.c, s, "@d", t, "Asia/Tehran")
            t += timedelta(hours=1)
        self.assertEqual(self.row()["status"], "failed")
        self.assertTrue(publisher.retry(self.c, self.row()["id"], "ed"))
        self.assertEqual(self.row()["status"], "queued")

    def test_permanent_error_fails_immediately_and_logs(self):
        publisher.dispatch_due(self.c, MockSender([PermanentError("chat not found")]), "@d", NOW, "Asia/Tehran")
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM event_log WHERE level='ERROR'").fetchone()[0], 1)

    def test_crash_mid_send_becomes_uncertain_not_resent(self):
        self.c.execute("UPDATE outbox SET status='sending', lease_until=?", (iso(NOW - timedelta(minutes=5)),))
        s = MockSender()
        publisher.dispatch_due(self.c, s, "@d", NOW, "Asia/Tehran")
        self.assertEqual((self.row()["status"], s.calls), ("uncertain", []))
        self.assertFalse(publisher.retry(self.c, self.row()["id"], "ed"))               # needs explicit confirmation
        self.assertTrue(publisher.retry(self.c, self.row()["id"], "ed", confirm_uncertain=True))

    def test_quiet_hours_defer(self):
        db.set_setting(self.c, "quiet_hours", "11:00-14:00")   # 12:30 Tehran
        s = MockSender()
        publisher.dispatch_due(self.c, s, "@d", NOW, "Asia/Tehran")
        self.assertEqual((s.calls, self.row()["status"]), ([], "queued"))
        publisher.dispatch_due(self.c, s, "@d", NOW + timedelta(hours=2), "Asia/Tehran")
        self.assertEqual(self.row()["status"], "sent")

    def test_quiet_hours_across_midnight(self):
        db.set_setting(self.c, "quiet_hours", "23:00-07:00")
        self.assertTrue(publisher.in_quiet_hours(self.c, NOW.replace(hour=21), "Asia/Tehran"))   # 00:30 local
        self.assertFalse(publisher.in_quiet_hours(self.c, NOW, "Asia/Tehran"))

    def test_min_interval_between_posts(self):
        publisher.enqueue(self.c, kind="alert", category="currency", text="two", buttons=[], key="a2",
                          record_ids=[], confidence=1, now=NOW, approved_by="ed")
        db.set_setting(self.c, "min_interval_seconds", 60)
        s = MockSender()
        publisher.dispatch_due(self.c, s, "@d", NOW, "Asia/Tehran")
        publisher.dispatch_due(self.c, s, "@d", NOW + timedelta(seconds=5), "Asia/Tehran")
        self.assertEqual(len(s.calls), 1)
        publisher.dispatch_due(self.c, s, "@d", NOW + timedelta(seconds=70), "Asia/Tehran")
        self.assertEqual(len(s.calls), 2)


class Opportunities(unittest.TestCase):
    def setUp(self):
        self.c = make_db()
        self.a = add_source(self.c, "a")

    def test_score_is_weighted_and_bounded(self):
        w = {"magnitude": 0.5, "confidence": 0.5}
        sc, br = opportunities.score({"magnitude": 2.0, "confidence": 0.5}, w)   # components clipped to [0,1]
        self.assertEqual(sc, 75.0)
        self.assertEqual({b["component"] for b in br}, {"magnitude", "confidence"})

    def test_no_opportunity_below_threshold(self):
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 60)
        post(self.c, self.a, "2", "دلار ۴۹٬۸۰۰ تومان", 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 0)

    def test_configurable_threshold(self):
        db.set_setting(self.c, "drop_threshold_pct", 0.2)
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 60)
        post(self.c, self.a, "2", "دلار ۴۹٬۸۰۰ تومان", 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)

    def test_deal_unverified_reference_is_labelled_and_scored_lower(self):
        post(self.c, self.a, "1", "هدفون تخفیف ۴۰٪ قیمت قبلی ۵٬۰۰۰٬۰۰۰ تومان قیمت جدید ۳٬۰۰۰٬۰۰۰ تومان تا ۲۴ ساعت دیگر", 5)
        o = self.c.execute("SELECT * FROM opportunities").fetchone()
        ev = json.loads(o["evidence"])
        self.assertEqual(ev["reference_kind"], "seller_stated_unverified")
        self.assertIn("تأییدنشده", o["explanation"])
        self.assertIsNotNone(o["expires_at"])

    def test_expired_deal_not_reported_and_expiry_job(self):
        post(self.c, self.a, "1", "هدفون تخفیف ۴۰٪ قیمت قبلی ۵٬۰۰۰٬۰۰۰ تومان قیمت جدید ۳٬۰۰۰٬۰۰۰ تومان تا ۱ ساعت دیگر", 5)
        self.assertEqual(opportunities.expire_old(self.c, NOW + timedelta(hours=2)), 1)

    def test_car_below_comparables_needs_enough_comparables(self):
        t = "پژو ۲۰۶ تیپ ۵ مدل ۱۴۰۲ تهران {} میلیون تومان"
        post(self.c, self.a, "1", t.format(400), 50)
        post(self.c, self.a, "2", t.format(402), 40)
        post(self.c, self.a, "3", t.format(300), 30)           # only 2 comparables -> nothing
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 0)
        post(self.c, self.a, "4", t.format(398), 20)
        post(self.c, self.a, "5", t.format(250), 10)
        o = self.c.execute("SELECT * FROM opportunities WHERE kind='car_below_comparables'").fetchone()
        self.assertIsNotNone(o)


if __name__ == "__main__":
    unittest.main()
