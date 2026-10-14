import re
import unittest

from werkzeug.security import generate_password_hash

from marketintel import db, publisher
from marketintel.admin.app import create_app
from marketintel.config import Config
from marketintel.util import iso

from .helpers import NOW, add_source, make_db, post, rec

CFG = Config(":memory:", "Asia/Tehran", "INFO", "", "@dest", False, "test-secret-key", "127.0.0.1", 8080, False)


class Admin(unittest.TestCase):
    def setUp(self):
        self.c = make_db()
        for u, role in (("adm", "admin"), ("edt", "editor"), ("vwr", "viewer")):
            self.c.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?)",
                           (u, generate_password_hash("correct-horse-battery"), role, iso(NOW)))
        self.a = add_source(self.c, "a")
        post(self.c, self.a, "1", "دلار ۵۰٬۰۰۰ تومان", 90)
        (self.j,) = post(self.c, self.a, "2", "دلار ۶۵٬۰۰۰ تومان", 0)      # needs_review
        self.app = create_app(CFG, self.c)

    def client(self, user=None, pw="correct-horse-battery"):
        cl = self.app.test_client()
        cl.get("/login")
        if user:
            r = cl.post("/login", data={"username": user, "password": pw, "_csrf": self.csrf(cl)})
            self.assertEqual(r.status_code, 302, r.data[:200])
        return cl

    def csrf(self, cl):
        with cl.session_transaction() as s:
            return s.get("csrf", "")

    def test_unauthenticated_cannot_see_anything(self):
        cl = self.app.test_client()
        for path in ("/", "/sources", "/records", "/records.csv", "/settings", "/publications", "/logs", "/review"):
            r = cl.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"])
        r = cl.post(f"/records/{self.j}/review", data={"decision": "approve"})
        self.assertEqual(r.status_code, 400)                              # CSRF guard fires before anything else

    def test_unauthenticated_post_with_csrf_is_401(self):
        cl = self.app.test_client()
        cl.get("/login")
        r = cl.post(f"/records/{self.j}/review", data={"decision": "approve", "_csrf": self.csrf(cl)})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(rec(self.c, self.j)["status"], "needs_review")

    def test_login_failure_and_audit(self):
        cl = self.app.test_client()
        cl.get("/login")
        r = cl.post("/login", data={"username": "adm", "password": "wrong", "_csrf": self.csrf(cl)})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='login_failed'").fetchone()[0], 1)
        self.assertEqual(cl.get("/").status_code, 302)

    def test_login_rate_limit(self):
        cl = self.app.test_client()
        cl.get("/login")
        for _ in range(8):
            cl.post("/login", data={"username": "adm", "password": "x", "_csrf": self.csrf(cl)})
        r = cl.post("/login", data={"username": "adm", "password": "correct-horse-battery", "_csrf": self.csrf(cl)})
        self.assertEqual(r.status_code, 429)

    def test_csrf_required_on_authenticated_post(self):
        cl = self.client("edt")
        self.assertEqual(cl.post(f"/records/{self.j}/review", data={"decision": "approve"}).status_code, 400)
        self.assertEqual(cl.post(f"/records/{self.j}/review", data={"decision": "approve", "_csrf": "bad"}).status_code, 400)
        self.assertEqual(rec(self.c, self.j)["status"], "needs_review")

    def test_viewer_is_read_only(self):
        cl = self.client("vwr")
        self.assertEqual(cl.get("/records").status_code, 200)
        t = self.csrf(cl)
        self.assertEqual(cl.post(f"/records/{self.j}/review", data={"decision": "approve", "_csrf": t}).status_code, 403)
        self.assertEqual(cl.post("/settings", data={"paused": "1", "_csrf": t}).status_code, 403)
        self.assertEqual(cl.post("/emergency-pause", data={"_csrf": t}).status_code, 403)
        self.assertEqual(cl.post("/sources/1/toggle", data={"_csrf": t}).status_code, 403)
        self.assertEqual(rec(self.c, self.j)["status"], "needs_review")

    def test_editor_can_review_but_not_change_settings_or_sources(self):
        cl = self.client("edt")
        t = self.csrf(cl)
        self.assertEqual(cl.post("/settings", data={"publish_mode": "auto", "_csrf": t}).status_code, 403)
        self.assertEqual(cl.post("/sources/1/toggle", data={"_csrf": t}).status_code, 403)
        r = cl.post(f"/records/{self.j}/review", data={"decision": "approve", "_csrf": t})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(rec(self.c, self.j)["status"], "valid")
        self.assertEqual(self.c.execute("SELECT reviewed_by FROM records WHERE id=?", (self.j,)).fetchone()[0], "edt")

    def test_editor_emergency_pause_and_resume(self):
        cl = self.client("edt")
        t = self.csrf(cl)
        cl.post("/emergency-pause", data={"_csrf": t})
        self.assertEqual(db.get_setting(self.c, "paused"), 1)
        self.assertIn("توقف اضطراری".encode(), cl.get("/").data)
        cl.post("/emergency-pause", data={"_csrf": t, "resume": "1"})
        self.assertEqual(db.get_setting(self.c, "paused"), 0)

    def test_admin_settings_validation_and_audit(self):
        cl = self.client("adm")
        t = self.csrf(cl)
        self.assertEqual(cl.post("/settings", data={"publish_mode": "yolo", "_csrf": t}).status_code, 400)
        self.assertEqual(cl.post("/settings", data={"alert_change_pct": "abc", "_csrf": t}).status_code, 400)
        r = cl.post("/settings", data={"publish_mode": "auto", "alert_change_pct": "3.5", "_csrf": t})
        self.assertEqual(r.status_code, 302)
        self.assertEqual((db.get_setting(self.c, "publish_mode"), db.get_setting(self.c, "alert_change_pct")), ("auto", 3.5))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='settings_change'").fetchone()[0], 1)
        self.assertNotIn(b"_bot_api_offset", cl.get("/settings").data)

    def test_source_toggle_and_edit_validation(self):
        cl = self.client("adm")
        t = self.csrf(cl)
        cl.post(f"/sources/{self.a}/toggle", data={"_csrf": t})
        self.assertEqual(self.c.execute("SELECT enabled FROM sources WHERE id=?", (self.a,)).fetchone()[0], 0)
        self.assertEqual(cl.post(f"/sources/{self.a}/edit", data={"reliability": "7", "_csrf": t}).status_code, 400)
        self.assertEqual(cl.post(f"/sources/{self.a}/edit", data={"reliability": "0.5", "default_unit": "EUR", "_csrf": t}).status_code, 400)
        cl.post(f"/sources/{self.a}/edit", data={"reliability": "0.5", "default_unit": "IRT", "categories": "currency, gold", "_csrf": t})
        row = self.c.execute("SELECT * FROM sources WHERE id=?", (self.a,)).fetchone()
        self.assertEqual((row["reliability"], row["default_unit"], row["categories"]), (0.5, "IRT", '["currency", "gold"]'))

    def test_pages_render_for_logged_in_user(self):
        publisher.enqueue(self.c, kind="alert", category="currency", text="<b>x</b>", buttons=[], key="z", record_ids=[],
                          confidence=1, now=NOW)
        cl = self.client("vwr")
        for path in ("/", "/sources", "/categories", "/records", "/records?status=valid&q=دلار", "/review", "/duplicates",
                     "/opportunities", "/publications", "/templates", "/history", "/history?entity_key=currency:usd:-:-", "/logs",
                     "/raw", "/settings"):
            r = cl.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn('dir="rtl"', r.get_data(as_text=True))

    def test_untrusted_message_text_is_escaped_in_dashboard(self):
        post(self.c, self.a, "9", "<script>alert(1)</script>")
        html = self.client("vwr").get("/raw").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_csv_export_neutralises_formula_injection(self):
        self.c.execute("UPDATE records SET asset_name='=HYPERLINK(\"http://evil\")' WHERE id=?", (self.j,))
        cl = self.client("vwr")
        r = cl.get("/records.csv")
        body = r.get_data(as_text=True)
        self.assertEqual(r.mimetype, "text/csv")
        self.assertIn("'=HYPERLINK", body)
        self.assertNotRegex(body, r",=HYPERLINK")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM audit_log WHERE action='export_csv'").fetchone()[0], 1)

    def test_outbox_actions(self):
        oid = publisher.enqueue(self.c, kind="alert", category="currency", text="t", buttons=[], key="m", record_ids=[],
                                confidence=1, now=NOW)
        cl = self.client("edt")
        t = self.csrf(cl)
        cl.post(f"/publications/{oid}/approve", data={"_csrf": t})
        self.assertEqual(self.c.execute("SELECT status, approved_by FROM outbox WHERE id=?", (oid,)).fetchone()[:], ("queued", "edt"))
        cl.post(f"/publications/{oid}/cancel", data={"_csrf": t})
        self.assertEqual(self.c.execute("SELECT status FROM outbox WHERE id=?", (oid,)).fetchone()[0], "cancelled")
        self.assertEqual(cl.post(f"/publications/{oid}/delete", data={"_csrf": t}).status_code, 404)

    def test_security_headers_and_healthz(self):
        cl = self.app.test_client()
        r = cl.get("/healthz")
        self.assertEqual((r.status_code, r.get_json()), (200, {"status": "ok"}))
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])

    def test_refuses_to_start_with_default_secret(self):
        bad = Config(":memory:", "Asia/Tehran", "INFO", "", "", False, "change-me-to-a-long", "127.0.0.1", 8080, False)
        with self.assertRaises(RuntimeError):
            create_app(bad)

    def test_deactivated_user_loses_access(self):
        cl = self.client("edt")
        self.c.execute("UPDATE users SET active=0 WHERE username='edt'")
        self.assertEqual(cl.get("/records").status_code, 401)


if __name__ == "__main__":
    unittest.main()
