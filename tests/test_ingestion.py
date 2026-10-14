import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

import requests

from marketintel import db, publisher
from marketintel.adapters import bot_api, mtproto
from marketintel.config import Config
from marketintel.logging_setup import redact
from marketintel.runner import purge_retention, run_cycle, source_watchdog
from marketintel.senders import MockSender, PermanentError, RateLimited, TelegramSender, TransientError
from marketintel.util import UTC, iso

from .helpers import NOW, add_source, make_db

TOKEN = "123456789:AAH_fake_token_value_0123456789abcdefgh"
CFG = Config(":memory:", "Asia/Tehran", "INFO", "", "@dest", False, "k", "127.0.0.1", 8080, False)


def upd(uid, chat, mid, text, minutes_ago=0, edited=False, username=None, **extra):
    c = {"id": chat, "type": "channel"}
    if username:
        c["username"] = username
    key = "edited_channel_post" if edited else "channel_post"
    return {"update_id": uid, key: {"message_id": mid, "date": int((NOW - timedelta(minutes=minutes_ago)).timestamp()),
                                    "chat": c, "text": text, **extra}}


class FakeResp:
    def __init__(self, code, body, reason="x"):
        self.status_code, self._b, self.reason = code, body, reason

    def json(self):
        if self._b is None:
            raise ValueError
        return self._b


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class BotIngestion(unittest.TestCase):
    def setUp(self):
        self.c = make_db()
        self.a = add_source(self.c, "a", chat_ref="@chan_a", access_method="bot_api")
        self.b = add_source(self.c, "b", chat_ref="-1002", access_method="bot_api")

    def test_update_mapping_text_caption_forward_url(self):
        chat, raw = bot_api.update_to_raw(upd(1, -1, 5, "دلار ۵۰٬۰۰۰ تومان", username="chan_a"))
        self.assertEqual((raw.external_id, raw.url, raw.text), ("5", "https://t.me/chan_a/5", "دلار ۵۰٬۰۰۰ تومان"))
        u = upd(2, -1, 6, "", username="x")
        del u["channel_post"]["text"]
        u["channel_post"]["caption"] = "کپشن دلار ۵۰ تومان"
        u["channel_post"]["photo"] = [{}]
        u["channel_post"]["forward_origin"] = {"type": "channel", "chat": {"id": -9, "username": "orig"}}
        _, raw = bot_api.update_to_raw(u)
        self.assertEqual((raw.text, raw.forward_from, raw.raw["has_media"]), ("کپشن دلار ۵۰ تومان", "@orig", True))
        self.assertIsNone(bot_api.update_to_raw({"update_id": 3, "message": {}}))

    def test_poll_maps_sources_ignores_unmapped_and_is_resumable(self):
        client = bot_api.FixtureBotClient([
            upd(10, -1, 1, "دلار ۵۰٬۰۰۰ تومان", 10, username="chan_a"),
            upd(11, -1002, 1, "یورو ۵۵٬۰۰۰ تومان", 9),
            upd(12, -777, 1, "دلار ۵۰٬۰۰۰ تومان", 8),                      # chat we have no arrangement with
            upd(13, -1, 1, "دلار ۵۰٬۵۰۰ تومان", 1, edited=True, username="chan_a"),
        ])
        st = bot_api.poll_once(self.c, client, NOW)
        self.assertEqual((st["new"], st["edit"], st["unmapped"]), (2, 1, 1))
        self.assertEqual(db.get_setting(self.c, "_bot_api_offset"), 14)
        st2 = bot_api.poll_once(self.c, client, NOW)                      # nothing re-delivered
        self.assertEqual(st2["updates"], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0], 3)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM records WHERE status='superseded'").fetchone()[0], 1)

    def test_disabled_source_is_not_ingested(self):
        self.c.execute("UPDATE sources SET enabled=0 WHERE id=?", (self.a,))
        client = bot_api.FixtureBotClient([upd(1, -1, 1, "دلار ۵۰٬۰۰۰ تومان", username="chan_a")])
        st = bot_api.poll_once(self.c, client, NOW)
        self.assertEqual(st["unmapped"], 1)

    def test_failure_recovery_no_loss_and_health_transitions(self):
        client = bot_api.FixtureBotClient([upd(1, -1002, 1, "دلار ۵۰٬۰۰۰ تومان", 5)], fail_times=3)
        for i in range(3):
            with self.assertRaises(ConnectionError):
                bot_api.poll_once(self.c, client, NOW)
        h = self.c.execute("SELECT health, consecutive_failures, last_error FROM sources WHERE id=?", (self.b,)).fetchone()
        self.assertEqual((h[0], h[1]), ("failing", 3))
        self.assertTrue(h[2])
        st = bot_api.poll_once(self.c, client, NOW)                       # network back: update still delivered
        self.assertEqual(st["new"], 1)
        h = self.c.execute("SELECT health, consecutive_failures FROM sources WHERE id=?", (self.b,)).fetchone()
        self.assertEqual((h[0], h[1]), ("ok", 0))

    def test_crash_between_ingest_and_offset_does_not_duplicate(self):
        u = upd(1, -1002, 1, "دلار ۵۰٬۰۰۰ تومان", 5)
        bot_api.poll_once(self.c, bot_api.FixtureBotClient([u]), NOW)
        db.set_setting(self.c, "_bot_api_offset", 0)                      # simulate offset lost in a crash
        st = bot_api.poll_once(self.c, bot_api.FixtureBotClient([u]), NOW)
        self.assertEqual((st["unchanged"], self.c.execute("SELECT COUNT(*) FROM records").fetchone()[0]), (1, 1))

    def test_run_cycle_survives_ingestion_failure_and_still_publishes(self):
        publisher.enqueue(self.c, kind="alert", category="currency", text="queued", buttons=[], key="q",
                          record_ids=[], confidence=1, now=NOW, approved_by="ed")
        s = MockSender()
        out = run_cycle(self.c, CFG, bot_api.FixtureBotClient([], fail_times=1), s, NOW)
        self.assertIn("ingest_error", out)
        self.assertEqual(out["publish"]["sent"], 1)

    def test_watchdog_flags_silent_stall(self):
        self.c.execute("UPDATE sources SET last_success_at=?, poll_interval_s=30 WHERE id=?",
                       (iso(NOW - timedelta(hours=2)), self.a))
        self.assertEqual(source_watchdog(self.c, NOW), ["a"])
        self.assertEqual(self.c.execute("SELECT health FROM sources WHERE id=?", (self.a,)).fetchone()[0], "failing")

    def test_retention_scrubs_raw_text_only(self):
        bot_api.poll_once(self.c, bot_api.FixtureBotClient([upd(1, -1002, 1, "دلار ۵۰٬۰۰۰ تومان", 5)]), NOW)
        self.c.execute("UPDATE sources SET retention_days=1")
        self.assertEqual(purge_retention(self.c, NOW + timedelta(days=3)), 1)
        self.assertEqual(self.c.execute("SELECT text FROM raw_messages").fetchone()[0], "[purged]")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1)

    def test_end_to_end_provenance_ingest_to_publish(self):
        db.set_setting(self.c, "publish_mode", "auto")
        client = bot_api.FixtureBotClient([
            upd(1, -1, 1, "دلار ۵۰٬۰۰۰ تومان", 60, username="chan_a"),
            upd(2, -1, 2, "دلار ۵۲٬۰۰۰ تومان", 1, username="chan_a")])
        s = MockSender()
        run_cycle(self.c, CFG, client, s, NOW)
        self.assertEqual(len(s.calls), 1)
        text = s.calls[0]["text"]
        self.assertIn("۵۲٬۰۰۰ تومان", text)
        ob = self.c.execute("SELECT * FROM outbox WHERE status='sent'").fetchone()
        (rid,) = json.loads(ob["record_ids"])
        url = self.c.execute("SELECT w.url FROM records r JOIN raw_messages w ON w.id=r.raw_id WHERE r.id=?", (rid,)).fetchone()[0]
        self.assertEqual(url, "https://t.me/chan_a/2")
        self.assertEqual(s.calls[0]["buttons"][0][0]["url"], url)
        self.assertEqual(s.calls[0]["chat_id"], "@dest")

    def test_mtproto_mapping_and_missing_dependency(self):
        msg = SimpleNamespace(id=7, message="دلار ۵۰٬۰۰۰ تومان", date=datetime(2026, 10, 14, 9, 0), edit_date=None, fwd_from=None)
        raw = mtproto.message_to_raw(msg, "chan_a")
        self.assertEqual((raw.external_id, raw.url, raw.message_date.tzinfo), ("7", "https://t.me/chan_a/7", UTC))
        self.c.execute("UPDATE sources SET access_method='mtproto' WHERE id=?", (self.a,))
        try:
            import telethon  # noqa: F401
        except ImportError:
            with self.assertRaises(mtproto.NotConfigured):
                mtproto.poll_once(self.c, 1, "h", "s")


class Http(unittest.TestCase):
    def test_http_bot_client_and_error_redaction(self):
        s = FakeSession(FakeResp(200, {"ok": True, "result": [{"update_id": 1}]}))
        self.assertEqual(bot_api.HttpBotClient(TOKEN, s).get_updates(0), [{"update_id": 1}])
        self.assertEqual(s.calls[0][1]["allowed_updates"], ["channel_post", "edited_channel_post"])
        s = FakeSession(FakeResp(409, {"ok": False, "description": f"conflict {TOKEN}"}))
        with self.assertRaises(ConnectionError) as cm:
            bot_api.HttpBotClient(TOKEN, s).get_updates(0)
        self.assertNotIn(TOKEN, str(cm.exception))
        s = FakeSession(requests.ConnectionError(f"failed for {TOKEN}"))
        with self.assertRaises(ConnectionError) as cm:
            bot_api.HttpBotClient(TOKEN, s).get_updates(0)
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_telegram_sender_paths(self):
        s = FakeSession(FakeResp(200, {"ok": True, "result": {"message_id": 42}}))
        snd = TelegramSender(TOKEN, s)
        self.assertEqual(snd.send("@d", "<b>x</b>", [[{"text": "t", "url": "https://e.x"}]]), "42")
        url, payload = s.calls[0]
        self.assertEqual((payload["parse_mode"], payload["chat_id"]), ("HTML", "@d"))
        self.assertIn("inline_keyboard", payload["reply_markup"])
        self.assertTrue(url.endswith("/sendMessage"))
        with self.assertRaises(RateLimited) as cm:
            TelegramSender(TOKEN, FakeSession(FakeResp(429, {"ok": False, "parameters": {"retry_after": 17}}))).send("@d", "x", [])
        self.assertEqual(cm.exception.retry_after, 17)
        with self.assertRaises(TransientError):
            TelegramSender(TOKEN, FakeSession(FakeResp(502, None))).send("@d", "x", [])
        with self.assertRaises(PermanentError) as cm:
            TelegramSender(TOKEN, FakeSession(FakeResp(400, {"ok": False, "description": f"bad {TOKEN}"}))).send("@d", "x", [])
        self.assertNotIn(TOKEN, str(cm.exception))
        with self.assertRaises(TransientError) as cm:
            TelegramSender(TOKEN, FakeSession(requests.Timeout(TOKEN))).send("@d", "x", [])
        self.assertNotIn(TOKEN, str(cm.exception))
        with self.assertRaises(ValueError):
            TelegramSender("")

    def test_message_length_capped(self):
        s = FakeSession(FakeResp(200, {"ok": True, "result": {"message_id": 1}}))
        TelegramSender(TOKEN, s).send("@d", "x" * 9000, [])
        self.assertEqual(len(s.calls[0][1]["text"]), 4096)

    def test_redaction_helper(self):
        for raw in (f"url https://api.telegram.org/bot{TOKEN}/sendMessage", f"token={TOKEN}", "password=hunter2",
                    '{"session": "abc123"}'):
            self.assertNotRegex(redact(raw), r"AAH_fake|hunter2|abc123")


if __name__ == "__main__":
    unittest.main()
