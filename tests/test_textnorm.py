import datetime
import unittest
from decimal import Decimal

from marketintel.jalali import gregorian_to_jalali, jalali_to_gregorian
from marketintel.textnorm import extract_numbers, normalize_for_match, normalize_text, to_persian_digits


class TextNorm(unittest.TestCase):
    def test_persian_and_arabic_digits(self):
        self.assertEqual(normalize_text("۱۲۳۴۵۶۷۸۹۰"), "1234567890")
        self.assertEqual(normalize_text("٠١٢٣٤٥٦٧٨٩"), "0123456789")

    def test_letter_variants(self):
        self.assertEqual(normalize_text("كيف"), "کیف")
        self.assertEqual(normalize_for_match("طلای\u200c۱۸ عيار"), "طلای 18 عیار")

    def test_thousand_and_decimal_separators(self):
        for s in ("۶۱٬۲۰۰", "۶۱،۲۰۰", "61,200"):
            t = extract_numbers(normalize_text(s))
            self.assertEqual(t[0].value, Decimal(61200), s)
        self.assertEqual(extract_numbers(normalize_text("۱۲٫۵"))[0].value, Decimal("12.5"))

    def test_ambiguous_dot_separator_flagged(self):
        self.assertIn("ambiguous_separator", extract_numbers("61.200")[0].flags)

    def test_units_and_scale_words(self):
        t = extract_numbers(normalize_text("۲۵ میلیون تومان"))[0]
        self.assertEqual((t.value, t.unit, t.scale_word), (Decimal(25_000_000), "IRT", "میلیون"))
        self.assertEqual(extract_numbers(normalize_text("۵۰۰ ریال"))[0].unit, "IRR")
        self.assertEqual(extract_numbers(normalize_text("۳ هزار تومن"))[0].value, Decimal(3000))
        self.assertIsNone(extract_numbers("2500")[0].unit)

    def test_time_date_and_percent_not_prices(self):
        toks = extract_numbers(normalize_text("ساعت ۱۴:۳۰ تاریخ ۱۴۰۵/۰۷/۲۰ تخفیف ۳۰٪ قیمت ۵۰۰ تومان"))
        prices = [t for t in toks if not t.is_percent]
        self.assertEqual([t.value for t in prices], [Decimal(500)])
        self.assertTrue(any(t.is_percent for t in toks))

    def test_persian_digit_rendering(self):
        self.assertEqual(to_persian_digits("1,234"), "۱,۲۳۴")

    def test_jalali_known_dates_and_roundtrip(self):
        self.assertEqual(gregorian_to_jalali(2024, 3, 20), (1403, 1, 1))
        self.assertEqual(gregorian_to_jalali(2026, 10, 12), (1405, 7, 20))
        d0 = datetime.date(1995, 1, 1)
        for i in range(0, 15000, 7):
            d = d0 + datetime.timedelta(days=i)
            self.assertEqual(jalali_to_gregorian(*gregorian_to_jalali(d.year, d.month, d.day)), (d.year, d.month, d.day))


if __name__ == "__main__":
    unittest.main()
