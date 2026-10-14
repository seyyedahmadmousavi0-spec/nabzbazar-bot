import unittest
from datetime import datetime
from decimal import Decimal

from marketintel.parsing import SourceCtx, parse_message
from marketintel.util import UTC

MD = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)  # 12:30 Tehran


def P(text, **kw):
    return parse_message(text, kw.pop("parser", "generic"), SourceCtx(message_date=MD, **kw))


class Parsing(unittest.TestCase):
    def test_toman_stated(self):
        it = P("دلار: ۶۱٬۲۰۰ تومان").items[0]
        self.assertEqual((it.asset_key, it.amount, it.stated_unit, it.unit_rule), ("usd", Decimal(61200), "IRT", "stated:toman"))
        self.assertEqual(it.amount_canonical, Decimal(612000))  # rial = toman x 10, rule recorded

    def test_rial_stated(self):
        it = P("دلار ۶۱۲٬۰۰۰ ریال").items[0]
        self.assertEqual((it.stated_unit, it.amount_canonical), ("IRR", Decimal(612000)))

    def test_missing_unit_never_guessed(self):
        it = P("دلار ۲۵۰۰").items[0]
        self.assertIsNone(it.stated_unit)
        self.assertIsNone(it.amount_canonical)
        self.assertIn("missing_unit", it.flags)

    def test_source_default_unit_is_recorded_rule(self):
        it = P("دلار ۶۱۲۰۰", default_unit="IRT").items[0]
        self.assertEqual(it.unit_rule, "source_default:toman")
        self.assertIn("unit_not_in_price_token", it.flags)

    def test_message_header_unit(self):
        its = P("قیمت‌ها به تومان\nدلار ۶۱۲۰۰\nیورو ۶۶۵۰۰").items
        self.assertEqual({i.unit_rule for i in its}, {"message_header:toman"})

    def test_buy_sell_and_range(self):
        its = P("دلار خرید ۶۱٬۰۰۰ فروش ۶۱٬۲۰۰ تومان").items
        self.assertEqual([(i.side, i.amount) for i in its], [("buy", Decimal(61000)), ("sell", Decimal(61200))])
        r = P("یورو ۶۶٬۵۰۰ تا ۶۷٬۰۰۰ تومان").items[0]
        self.assertEqual((r.price_low, r.price_high, r.amount), (Decimal(66500), Decimal(67000), Decimal(66750)))
        self.assertIn("range_price", r.flags)

    def test_multiple_assets_on_one_line(self):
        its = P("دلار ۶۱٬۲۰۰ تومان | یورو ۶۶٬۵۰۰ تومان").items
        self.assertEqual([i.asset_key for i in its], ["usd", "eur"])

    def test_asset_alias_used_as_unit_is_not_an_asset(self):
        its = P("بیت کوین ۶۷٬۰۰۰ دلار").items
        self.assertEqual([(i.asset_key, i.stated_unit) for i in its], [("btc", "USD")])

    def test_gold_and_coin_variants_distinct(self):
        its = P("سکه امامی ۴۵ میلیون تومان\nسکه بهار آزادی ۴۰ میلیون تومان").items
        self.assertEqual([i.asset_key for i in its], ["coin_emami", "coin_bahar"])
        self.assertEqual(its[0].amount, Decimal(45_000_000))

    def test_car_listing(self):
        it = P("پژو ۲۰۶ تیپ ۵ مدل ۱۴۰۲ کارکرد ۴۵٬۰۰۰ تهران ۴۸۰ میلیون تومان").items[0]
        self.assertEqual((it.category, it.brand, it.model, it.variant, it.model_year, it.location, it.price_type),
                         ("car", "peugeot", "206", "tip5", 1402, "تهران", "asking_price"))
        self.assertEqual(it.amount, Decimal(480_000_000))
        self.assertEqual(it.extras["mileage_km"], 45000)

    def test_car_missing_variant_flagged(self):
        it = P("پژو ۲۰۶ ۴۸۰ میلیون تومان").items[0]
        self.assertIn("missing_variant", it.flags)

    def test_deal(self):
        it = P("هدفون تخفیف ۴۰٪ قیمت قبلی ۵٬۰۰۰٬۰۰۰ تومان قیمت جدید ۳٬۰۰۰٬۰۰۰ تومان کد: SAVE40 تا ۲۴ ساعت دیگر").items[0]
        self.assertEqual((it.category, it.amount, it.extras["old_price"], it.extras["coupon_code"],
                          it.extras["expires_in_hours"]), ("deal", Decimal(3_000_000), 5_000_000.0, "SAVE40", 24))
        self.assertNotIn("discount_inconsistent", it.flags)

    def test_deal_inconsistent_discount_flagged(self):
        it = P("هدفون تخفیف ۸۰٪ قیمت قبلی ۵٬۰۰۰٬۰۰۰ تومان قیمت جدید ۴٬۵۰۰٬۰۰۰ تومان").items[0]
        self.assertIn("discount_inconsistent", it.flags)

    def test_stated_time_earlier_today_and_future_means_yesterday(self):
        a = P("دلار ۶۱۲۰۰ تومان ساعت ۱۰:۰۰").items[0]
        self.assertEqual(a.observed_source, "stated_time")
        self.assertEqual(a.observed_at, datetime(2026, 10, 14, 6, 30, tzinfo=UTC))
        b = P("دلار ۶۱۲۰۰ تومان ساعت ۱۸:۰۰").items[0]
        self.assertEqual(b.observed_at, datetime(2026, 10, 13, 14, 30, tzinfo=UTC))

    def test_stated_jalali_date(self):
        it = P("دلار ۶۱۲۰۰ تومان تاریخ ۱۴۰۵/۰۷/۲۰").items[0]
        self.assertEqual(it.observed_source, "stated_date")
        self.assertEqual(it.observed_at.date().isoformat(), "2026-10-11")  # 00:00 Tehran = prev day UTC

    def test_unparsable_and_malformed(self):
        for t in ("", "   ", "امروز بازار آرام بود", "🔥🔥🔥", "دلار"):
            r = P(t)
            if t == "دلار":
                self.assertIn("missing_price", r.items[0].flags)
            else:
                self.assertEqual(r.status, "unparsed", t)

    def test_pipe_table_source_specific_parser(self):
        r = P("قیمت‌ها به تومان\nدلار | ۶۱٬۰۰۰ | ۶۱٬۲۰۰\nیورو | ۶۶٬۰۰۰ | ۶۶٬۵۰۰", parser="pipe_table")
        self.assertEqual([(i.asset_key, i.side, i.amount) for i in r.items][:2],
                         [("usd", "buy", Decimal(61000)), ("usd", "sell", Decimal(61200))])
        self.assertEqual(len(r.items), 4)

    def test_category_restriction_of_source(self):
        r = P("دلار ۶۱۲۰۰ تومان\nسکه امامی ۴۵ میلیون تومان", categories=("currency",))
        self.assertEqual([i.asset_key for i in r.items], ["usd"])
        self.assertEqual(r.status, "partial")


if __name__ == "__main__":
    unittest.main()
