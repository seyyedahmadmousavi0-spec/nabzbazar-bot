import json
import unittest


from marketintel import analytics, db, pipeline
from marketintel.pipeline import RawInput, ingest_raw, process_raw


from .helpers import NOW, add_source, make_db, post, rec


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.c = make_db()
        self.a = add_source(self.c, "a")
        self.b = add_source(self.c, "b", reliability=0.7)

    def test_persistence_and_raw_kept_separately(self):
        (rid,) = post(self.c, self.a, "1", "دلار: ۵۰٬۰۰۰ تومان")
        r = rec(self.c, rid)
        self.assertEqual((r["status"], r["amount"], r["amount_canonical"], r["canonical_unit"]), ("valid", 50000, 500000, "IRR"))
        raw = self.c.execute("SELECT * FROM raw_messages WHERE id=?", (r["raw_id"],)).fetchone()
        self.assertEqual(raw["text"], "دلار: ۵۰٬۰۰۰ تومان")  # original untouched
        self.assertEqual(raw["parse_status"], "parsed")
        self.assertEqual(raw["url"], "https://t.me/a/1")
        self.assertTrue(json.loads(r["confidence_detail"]))

    def test_ingest_is_idempotent(self):
        i = RawInput("9", "دلار ۵۰٬۰۰۰ تومان", NOW)
        r1 = ingest_raw(self.c, self.a, i, NOW)
        r2 = ingest_raw(self.c, self.a, i, NOW)
        self.assertEqual((r1[1], r2[1], r1[0]), ("new", "unchanged", r2[0]))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0], 1)

    def test_edit_creates_revision_and_supersedes(self):
        (r1,) = post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان")
        (r2,) = post(self.c, self.a, "1", "دلار ۵۰٬۵۰۰ تومان")
        self.assertEqual(rec(self.c, r1)["status"], "superseded")
        self.assertEqual(rec(self.c, r2)["status"], "valid")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0], 2)

    def test_deletion_retracts_and_cancels_unsent(self):
        (r1,) = post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 120)
        (r2,) = post(self.c, self.a, "2", "دلار ۵۲٬۰۰۰ تومان", 0)  # +4% -> alert queued for approval
        self.assertEqual(self.c.execute("SELECT status FROM outbox").fetchone()[0], "pending_approval")
        self.assertEqual(pipeline.mark_deleted(self.c, self.a, "2", NOW), 1)
        self.assertEqual(rec(self.c, r2)["status"], "retracted")
        self.assertEqual(self.c.execute("SELECT status FROM outbox").fetchone()[0], "cancelled")

    def test_same_source_repeat_is_duplicate_but_later_repeat_kept(self):
        (a,) = post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 30)
        (b,) = post(self.c, self.a, "2", "دلار ۵۰٬۰۰۰ تومان", 28)
        (c,) = post(self.c, self.a, "3", "دلار ۵۰٬۰۰۰ تومان", 0)
        self.assertEqual((rec(self.c, b)["status"], rec(self.c, b)["dup_of"], rec(self.c, b)["dup_reason"]),
                         ("duplicate", a, "same_source_repeat"))
        self.assertEqual(rec(self.c, c)["status"], "valid")  # still-current confirmation after the window

    def test_cross_source_syndicated_copy_detected(self):
        msg = "قیمت‌ها به تومان\nطلای ۱۸ عیار ۳٬۰۰۰٬۰۰۰ تومان\nسکه امامی ۴۰٬۰۰۰٬۰۰۰ تومان"
        (a, _) = post(self.c, self.a, "1", msg, 20)
        (b, _) = post(self.c, self.b, "1", msg, 10)
        self.assertEqual((rec(self.c, b)["status"], rec(self.c, b)["dup_of"], rec(self.c, b)["dup_reason"]),
                         ("duplicate", a, "syndicated_copy"))

    def test_forwarded_message_is_copy_even_if_short(self):
        (a,) = post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 10)
        (b,) = post(self.c, self.b, "1", "دلار ۵۰٬۰۰۰ تومان", 5, forward_from="@a")
        self.assertEqual(rec(self.c, b)["dup_reason"], "forwarded_copy")

    def test_short_independent_messages_are_not_merged(self):
        (a,) = post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 10)
        (b,) = post(self.c, self.b, "1", "دلار ۵۰٬۰۰۰ تومان", 5)
        self.assertEqual(rec(self.c, b)["status"], "valid")

    def test_distinct_vehicles_never_merged(self):
        base = "پژو ۲۰۶ {} مدل {} {} {} میلیون تومان"
        ids = [post(self.c, self.a, str(i), base.format(*args), 10 - i)[0] for i, args in enumerate([
            ("تیپ ۵", 1402, "تهران", 400), ("تیپ ۲", 1402, "تهران", 400),
            ("تیپ ۵", 1401, "تهران", 400), ("تیپ ۵", 1402, "مشهد", 400)])]
        self.assertEqual([rec(self.c, i)["status"] for i in ids], ["valid"] * 4)
        self.assertEqual(len({rec(self.c, i)["entity_key"] for i in ids}), 4)

    def test_same_car_listing_reposted_same_price_is_duplicate_different_price_is_not(self):
        t = "پژو ۲۰۶ تیپ ۵ مدل ۱۴۰۲ تهران {} میلیون تومان"
        (a,) = post(self.c, self.a, "1", t.format(400), 100)
        (b,) = post(self.c, self.a, "2", t.format(400), 50)
        (c,) = post(self.c, self.a, "3", t.format(402), 10)
        self.assertEqual(rec(self.c, b)["dup_reason"], "repost_same_source")
        self.assertEqual(rec(self.c, c)["status"], "valid")

    def test_conflicting_prices_both_kept_and_flagged_as_opportunity(self):
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 5)
        post(self.c, self.b, "1", "دلار ۵۲٬۰۰۰ تومان", 3)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM records WHERE status='valid'").fetchone()[0], 2)
        o = self.c.execute("SELECT * FROM opportunities WHERE kind='cross_source_spread'").fetchone()
        self.assertIsNotNone(o)
        ev = json.loads(o["evidence"])
        self.assertEqual(len(ev["observations"]), 2)
        self.assertIn("سود تضمین", o["assumptions"])
        self.assertTrue(all(x["url"] and x["observed_at"] for x in ev["observations"]))

    def test_stale_record_kept_as_history_but_not_alerting(self):
        (r,) = post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 60 * 24)
        self.assertEqual(rec(self.c, r)["status"], "stale")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_scale_error_goes_to_review_not_valid(self):
        post(self.c, self.a, "1", "یورو ۵۵٬۰۰۰ تومان", 100)
        (r,) = post(self.c, self.a, "2", "یورو ۵٬۵۰۰ تومان", 0)
        row = rec(self.c, r)
        self.assertEqual(row["status"], "needs_review")
        self.assertIn("possible_scale_error", row["validation"])

    def test_implausible_value_flagged(self):
        (r,) = post(self.c, self.a, "1", "دلار ۱۲ تومان")
        self.assertEqual(rec(self.c, r)["status"], "needs_review")
        self.assertIn("implausible_value", rec(self.c, r)["validation"])

    def test_missing_price_invalid_and_missing_unit_review(self):
        (r,) = post(self.c, self.a, "1", "دلار بالا رفت")
        self.assertEqual(rec(self.c, r)["status"], "invalid")
        (u,) = post(self.c, self.a, "2", "سکه امامی ۴۵۰۰۰۰۰۰")
        self.assertEqual(rec(self.c, u)["status"], "needs_review")
        self.assertIsNone(rec(self.c, u)["amount_canonical"])

    def test_unconfirmed_jump_vs_corroborated_jump(self):
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 100)
        post(self.c, self.a, "2", "دلار ۵۰٬۱۰۰ تومان", 90)
        (j,) = post(self.c, self.a, "3", "دلار ۶۵٬۰۰۰ تومان", 10)  # +30%, nobody confirms
        self.assertEqual(rec(self.c, j)["status"], "needs_review")
        self.assertIn("unconfirmed_jump", rec(self.c, j)["validation"])
        # another independent source reports the same level -> genuine move
        (k,) = post(self.c, self.b, "1", "دلار ۶۵٬۲۰۰ تومان", 5)
        self.assertEqual(rec(self.c, k)["status"], "valid")
        self.assertIn("unusual_move_corroborated", rec(self.c, k)["validation"])
        self.assertEqual(rec(self.c, j)["status"], "valid")  # earlier unconfirmed report released
        self.assertEqual(rec(self.c, j)["reviewed_by"], "auto:corroborated")

    def test_review_approve_runs_hooks_and_reject(self):
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 90)
        (j,) = post(self.c, self.a, "2", "دلار ۶۵٬۰۰۰ تومان", 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertTrue(pipeline.review_record(self.c, j, True, "ed", NOW))
        self.assertEqual(rec(self.c, j)["status"], "valid")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM outbox WHERE kind='alert'").fetchone()[0], 1)
        self.assertFalse(pipeline.review_record(self.c, j, False, "ed", NOW))  # no longer in review

    def test_unparsed_message_is_logged_not_lost(self):
        ids = post(self.c, self.a, "1", "سلام دوستان")
        self.assertEqual(ids, [])
        raw = self.c.execute("SELECT parse_status FROM raw_messages").fetchone()
        self.assertEqual(raw[0], "unparsed")

    def test_reprocess_supersedes_and_rebuilds(self):
        (r,) = post(self.c, self.a, "1", "سکه امامی ۴۵۰۰۰۰۰۰")
        raw_id = rec(self.c, r)["raw_id"]
        self.c.execute("UPDATE sources SET default_unit='IRT' WHERE id=?", (self.a,))
        (n,) = pipeline.reprocess_raw(self.c, raw_id, "ed", NOW)
        self.assertEqual(rec(self.c, r)["status"], "superseded")
        self.assertEqual(rec(self.c, n)["unit_rule"], "source_default:toman")


class Analytics(unittest.TestCase):
    def setUp(self):
        self.c = make_db()
        self.a = add_source(self.c, "a")

    def test_no_trend_invented_without_base_observation(self):
        (r,) = post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان")
        self.assertIsNone(analytics.change_over(self.c, rec(self.c, r), 60))
        s = analytics.daily_summary(self.c, "currency", NOW)
        self.assertIsNone(s[0]["pct_change"])
        self.assertEqual(s[0]["n_obs"], 1)

    def test_change_over_window(self):
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 60)
        (r,) = post(self.c, self.a, "2", "دلار ۵۱٬۰۰۰ تومان", 0)
        ch = analytics.change_over(self.c, rec(self.c, r), 60)
        self.assertAlmostEqual(ch.pct_change, 2.0)
        self.assertEqual(ch.abs_change, 10000)  # rial

    def test_changes_never_cross_entities(self):
        post(self.c, self.a, "1", "دلار خرید ۵۰٬۰۰۰ تومان", 60)
        (r,) = post(self.c, self.a, "2", "دلار فروش ۵۱٬۰۰۰ تومان", 0)
        self.assertIsNone(analytics.change_over(self.c, rec(self.c, r), 60))

    def test_robust_outlier_needs_enough_data(self):
        self.assertFalse(analytics.robust_outlier([1, 2, 3], 1000))
        self.assertTrue(analytics.robust_outlier([100, 101, 99, 100, 102, 98], 1000))

    def test_source_stats(self):
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان")
        post(self.c, self.a, "2", "بدون قیمت")
        st = analytics.source_stats(self.c)[0]
        self.assertEqual((st["records"], st["unparsed_messages"], st["valid_rate"]), (1, 1, 1.0))


if __name__ == "__main__":
    unittest.main()
