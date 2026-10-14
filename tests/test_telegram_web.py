import unittest
from datetime import datetime, timezone
from marketintel.adapters.telegram_web import extract_web_messages, poll_source
from marketintel.sources import upsert_source
from .helpers import make_db

SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="testchannel/101">
    <div class="tgme_widget_message_bubble">
      <time datetime="2026-10-14T11:00:00+00:00" class="time">11:00</time>
      <div class="tgme_widget_message_text">دلار فردایی 262,900 خرید 262,950 فروش</div>
    </div>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="testchannel/102">
    <div class="tgme_widget_message_bubble">
      <time datetime="2026-10-14T11:05:00+00:00" class="time">11:05</time>
      <div class="tgme_widget_message_text">بیت‌کوین: 83,000 دلار<br>تتر: 61,500 تومان</div>
    </div>
  </div>
</div>
</body>
</html>
"""

class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

class FakeSession:
    def __init__(self, text):
        self.text = text
    def get(self, url, headers=None, timeout=None):
        return FakeResponse(200, self.text)

class TelegramWebTests(unittest.TestCase):
    def test_extract_web_messages(self):
        items = extract_web_messages(SAMPLE_HTML, "testchannel")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].external_id, "101")
        self.assertIn("262,900", items[0].text)
        self.assertEqual(items[1].external_id, "102")
        self.assertIn("بیت‌کوین", items[1].text)
        self.assertIn("تتر", items[1].text)

    def test_poll_source(self):
        c = make_db()
        sid = upsert_source(c, "test_ch", name="Test Ch", chat_ref="@testchannel", access_method="telegram_web", default_unit="IRT")
        row = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
        stats = poll_source(c, row, session=FakeSession(SAMPLE_HTML), now=datetime.now(timezone.utc))
        self.assertEqual(stats["new"], 2)
        row2 = c.execute("SELECT cursor FROM sources WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row2["cursor"], "102")

if __name__ == "__main__":
    unittest.main()

