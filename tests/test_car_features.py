import unittest
from decimal import Decimal
from marketintel.car_announcements import detect_car_announcement, render_car_announcement
from marketintel.publisher import render_car_bulletin
from .helpers import NOW, add_source, make_db, post, rec


class CarFeaturesTest(unittest.TestCase):
    def test_pre_order_announcement_detection(self):
        text = """🟢 اطلاعیه خودروهای وارداتی در سایت اتونوین ویژه مهر ماه 1405
▪️پیش ثبت فروش فوق العاده خودروی کرولا کراس مدل 2026 محصول شرکت کاوش خودرو 
▪️ مبلغ حساب وکالتی: 500 میلیون تومان
▪️مهلت وکالتی‌کردن حساب: 20 مهر ماه تا 23 مهر 1405 
▪️مهلت ثبت سفارش: 21 مهر ماه تا 24 مهر ماه 1405
▪️زمان آزادسازی مبالغ بلوکه شده : 25 مهر ماه 
🔺 فروش محصولات به صورت قرعه کشی میباشد"""
        ann = detect_car_announcement(text)
        self.assertIsNotNone(ann)
        self.assertEqual(ann["kind"], "pre_order")
        self.assertEqual(ann["deposit"], "500 میلیون تومان")
        self.assertTrue(ann["is_lottery"])

        rendered = render_car_announcement(ann, "@nabzbazar360")
        self.assertIn("اطلاعیه رسمی ثبت‌نام و شرایط فروش خودرو", rendered)
        self.assertIn("۵۰۰ میلیون تومان", rendered)
        self.assertIn("#شرایط_فروش", rendered)

    def test_factory_price_change_detection(self):
        text = """🔴 افزایش 1 میلیارد و 282 میلیون تومانی قیمت فیدلیتی الیت
🔻شرکت بهمن موتور قیمت جدید فیدلیتی الیت 7 نفره را اعلام کرد.
▪️قیمت قبلی: 5,176,000,000 تومان
▪️قیمت جدید: 6,458,000,000 تومان
▪️میزان افزایش: 1,282,000,000 تومان"""
        ann = detect_car_announcement(text)
        self.assertIsNotNone(ann)
        self.assertEqual(ann["kind"], "factory_price_change")

        rendered = render_car_announcement(ann, "@nabzbazar360")
        self.assertIn("اعلام قیمت جدید کارخانه", rendered)
        self.assertIn("۶,۴۵۸,۰۰۰,۰۰۰ تومان", rendered)
        self.assertIn("#قیمت_کارخانه", rendered)

    def test_car_bulletin_rendering(self):
        conn = make_db()
        sid = add_source(conn, "car_src")
        # Add a domestic car
        post(conn, sid, "1", "تارا(V4)توربو⬅️3.900")
        # Add a montazhi car
        post(conn, sid, "2", "فیدلیتی الیت⬅️7.440")
        # Add an imported car
        post(conn, sid, "3", "کرولا کراس⬅️9.680")
        # Add a commercial truck
        post(conn, sid, "4", "کشنده فوتون : ۲۳.۵۰۰")

        bulletin = render_car_bulletin(conn, tzname="Asia/Tehran", now=NOW)
        self.assertIn("نبض بازار خودرو", bulletin)
        self.assertIn("محصولات ایران‌خودرو و سایپا", bulletin)
        self.assertIn("تارا", bulletin)
        self.assertIn("خودروهای مونتاژی", bulletin)
        self.assertIn("فیدلیتی", bulletin)
        self.assertIn("خودروهای وارداتی", bulletin)
        self.assertIn("تویوتا", bulletin)
        self.assertIn("وانت، پیکاپ و خودروهای تجاری / سنگین", bulletin)
        self.assertIn("کشنده فوتون", bulletin)

    def test_schedule_car_bulletin(self):
        import marketintel.db as db
        from marketintel.publisher import schedule_car_bulletin
        conn = make_db()
        db.set_setting(conn, "publish_mode", "auto")

        # 1. auto_car_bulletin_interval_hours is 0 -> should not schedule
        db.set_setting(conn, "auto_car_bulletin_interval_hours", 0)
        self.assertIsNone(schedule_car_bulletin(conn, NOW, "Asia/Tehran"))

        # 2. auto_car_bulletin_interval_hours is 6, but no car records -> should not schedule
        db.set_setting(conn, "auto_car_bulletin_interval_hours", 6)
        self.assertIsNone(schedule_car_bulletin(conn, NOW, "Asia/Tehran"))

        # 3. Add car records -> should schedule and return outbox ID
        sid = add_source(conn, "car_src")
        post(conn, sid, "1", "تارا(V4)توربو⬅️3.900")
        outbox_id = schedule_car_bulletin(conn, NOW, "Asia/Tehran")
        self.assertIsNotNone(outbox_id)

        # Verify queued in outbox
        row = conn.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
        self.assertEqual(row["kind"], "car_bulletin")
        self.assertEqual(row["category"], "car")
        self.assertEqual(row["status"], "queued")

        # 4. Same interval window -> should not schedule duplicate
        self.assertIsNone(schedule_car_bulletin(conn, NOW, "Asia/Tehran"))

    def test_profit_margin_with_db_records(self):
        conn = make_db()
        sid = add_source(conn, "car_src")
        # free market price is 7,440,000,000
        post(conn, sid, "1", "فیدلیتی الیت⬅️7.440")

        text = """🔴 افزایش قیمت فیدلیتی الیت
▪️قیمت قبلی: 5,176,000,000 تومان
▪️قیمت جدید: 6,458,000,000 تومان"""
        ann = detect_car_announcement(text, conn)
        self.assertIsNotNone(ann)
        self.assertEqual(ann["kind"], "factory_price_change")
        self.assertIsNotNone(ann["market_price"])
        self.assertIsNotNone(ann["profit_margin"])
        self.assertGreater(ann["profit_margin"], 0)
        rendered = render_car_announcement(ann, "@nabzbazar360")
        self.assertIn("سود ثبت‌نام", rendered)

