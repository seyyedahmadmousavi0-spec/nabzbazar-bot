"""Secure, RTL admin dashboard (Flask). Session auth, roles, CSRF on every POST, audit log."""
from __future__ import annotations

import csv
import hmac
import io
import json
import secrets
import sqlite3
from datetime import timedelta
from functools import wraps
from typing import Any, Callable

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash

from .. import analytics, pipeline, publisher
from ..config import Config
from ..db import DEFAULT_SETTINGS, all_settings, audit, connect, get_setting, migrate, set_setting, tx
from ..sources import set_enabled
from ..util import iso, jload, utcnow
from ..publisher import fmt_amount, fmt_time

ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}


def create_app(cfg: Config, conn: sqlite3.Connection | None = None) -> Flask:
    if not cfg.admin_secret_key or cfg.admin_secret_key.startswith("change-me"):
        if conn is None:  # tests inject their own conn and a key
            raise RuntimeError("MI_ADMIN_SECRET_KEY must be set to a long random value")
    app = Flask(__name__)
    app.config.update(SECRET_KEY=cfg.admin_secret_key, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=cfg.cookie_secure, PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                      MAX_CONTENT_LENGTH=1_000_000)
    shared = conn
    login_failures: dict[str, list[float]] = {}

    def db() -> sqlite3.Connection:
        if shared is not None:
            return shared
        if "db" not in g:
            g.db = connect(cfg.db_path)
            migrate(g.db)
        return g.db

    @app.teardown_appcontext
    def _close(_: Any) -> None:
        d = g.pop("db", None)
        if d is not None:
            d.close()

    @app.after_request
    def _headers(resp: Response) -> Response:
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "default-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: https:;"
        return resp

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return str(session["csrf"])

    app.jinja_env.filters["cats"] = lambda s: ",".join(jload(s, []))
    app.jinja_env.globals.update(csrf_token=csrf_token,
                                 fmt_amount=lambda r: fmt_amount(r, get_setting(db(), "display_unit"), get_setting(db(), "thousands_separator") or ","),
                                 fmt_time=lambda s: fmt_time(s, cfg.timezone) if s else "—")

    @app.before_request
    def _csrf_guard() -> None:
        if request.method == "POST" and request.endpoint != "static":
            sent = request.form.get("_csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "CSRF token missing or invalid")

    def require(role: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(fn)
            def inner(*a: Any, **k: Any) -> Any:
                u = session.get("user")
                if not u:
                    if request.method == "GET":
                        return redirect(url_for("login", next=request.path))
                    abort(401)
                row = db().execute("SELECT role, active FROM users WHERE username=?", (u,)).fetchone()
                if not row or not row["active"]:
                    session.clear()
                    abort(401)
                if ROLE_RANK[row["role"]] < ROLE_RANK[role]:
                    abort(403)
                g.user, g.role = u, row["role"]
                return fn(*a, **k)
            return inner
        return deco

    # ---- health check for cloud monitors / uptime -----------------------------------
    @app.route("/health")
    def health_check() -> Any:
        return jsonify({"status": "ok", "app": "marketintel", "time": iso(utcnow())})

    # ---- auth -----------------------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        if request.method == "POST":
            ip = request.remote_addr or "?"
            import time
            now = time.time()
            recent = [t for t in login_failures.get(ip, []) if now - t < 300]
            if len(recent) >= 8:
                abort(429, "too many attempts")
            row = db().execute("SELECT * FROM users WHERE username=? AND active=1",
                               (request.form.get("username", ""),)).fetchone()
            if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
                session.clear()
                session.permanent = True
                session["user"] = row["username"]
                csrf_token()
                audit(db(), row["username"], "login")
                return redirect(request.args.get("next") if (request.args.get("next") or "").startswith("/") else url_for("overview"))
            login_failures[ip] = recent + [now]
            audit(db(), request.form.get("username", "?")[:50], "login_failed", {"ip": ip})
            flash("نام کاربری یا گذرواژه نادرست است", "err")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout() -> Any:
        session.clear()
        return redirect(url_for("login"))

    @app.route("/healthz")
    def healthz() -> Any:
        """Unauthenticated, minimal: no data, no secrets."""
        try:
            db().execute("SELECT 1").fetchone()
            return jsonify(status="ok")
        except Exception:
            return jsonify(status="db_error"), 503

    # ---- pages ----------------------------------------------------------------------
    def page(title: str, **ctx: Any) -> str:
        return render_template("page.html", title=title, **ctx)

    @app.route("/")
    @require("viewer")
    def overview() -> Any:
        d = db()
        now = utcnow()
        q = lambda sql, *a: d.execute(sql, a).fetchone()[0]  # noqa: E731
        stats = {
            "sources_enabled": q("SELECT COUNT(*) FROM sources WHERE enabled=1"),
            "sources_failing": q("SELECT COUNT(*) FROM sources WHERE enabled=1 AND health IN ('failing','degraded')"),
            "raw_24h": q("SELECT COUNT(*) FROM raw_messages WHERE received_at>=?", iso(now - timedelta(hours=24))),
            "unparsed": q("SELECT COUNT(*) FROM raw_messages WHERE parse_status='unparsed'"),
            "review": q("SELECT COUNT(*) FROM records WHERE status='needs_review'"),
            "invalid": q("SELECT COUNT(*) FROM records WHERE status='invalid'"),
            "dups": q("SELECT COUNT(*) FROM records WHERE status='duplicate'"),
            "opps": q("SELECT COUNT(*) FROM opportunities WHERE status='pending'"),
            "pending_pub": q("SELECT COUNT(*) FROM outbox WHERE status='pending_approval'"),
            "queued": q("SELECT COUNT(*) FROM outbox WHERE status='queued'"),
            "failed_pub": q("SELECT COUNT(*) FROM outbox WHERE status IN ('failed','uncertain')"),
            "sent_24h": q("SELECT COUNT(*) FROM outbox WHERE status='sent' AND sent_at>=?", iso(now - timedelta(hours=24))),
            "errors_24h": q("SELECT COUNT(*) FROM event_log WHERE level='ERROR' AND ts>=?", iso(now - timedelta(hours=24))),
        }
        return page("نمای کلی", view="overview", stats=stats, settings=all_settings(d),
                    sources=d.execute("SELECT * FROM sources ORDER BY id").fetchall())

    def paged(sql: str, args: list[Any]) -> tuple[list[sqlite3.Row], int]:
        p = max(1, request.args.get("page", 1, type=int))
        rows = db().execute(sql + " LIMIT 50 OFFSET ?", [*args, (p - 1) * 50]).fetchall()
        return rows, p

    @app.route("/sources")
    @require("viewer")
    def sources() -> Any:
        return page("منابع", view="sources", rows=db().execute("SELECT * FROM sources ORDER BY id").fetchall(),
                    stats=analytics.source_stats(db()))

    @app.route("/sources/<int:sid>/toggle", methods=["POST"])
    @require("admin")
    def source_toggle(sid: int) -> Any:
        row = db().execute("SELECT enabled FROM sources WHERE id=?", (sid,)).fetchone() or abort(404)
        set_enabled(db(), sid, not row["enabled"], g.user)
        return redirect(url_for("sources"))

    @app.route("/sources/<int:sid>/edit", methods=["POST"])
    @require("admin")
    def source_edit(sid: int) -> Any:
        f = request.form
        try:
            rel = float(f.get("reliability", "0.7"))
            assert 0 <= rel <= 1
            unit = f.get("default_unit") or None
            assert unit in (None, "IRT", "IRR")
            cats = [c.strip() for c in f.get("categories", "").split(",") if c.strip()]
        except (ValueError, AssertionError):
            abort(400, "invalid values")
        db().execute("UPDATE sources SET reliability=?, default_unit=?, categories=?, attribution=?, restrictions=? "
                     "WHERE id=?", (rel, unit, json.dumps(cats, ensure_ascii=False), f.get("attribution", "")[:200],
                                    f.get("restrictions", "")[:500], sid))
        audit(db(), g.user, "source_edit", {"source_id": sid})
        return redirect(url_for("sources"))

    @app.route("/categories", methods=["GET", "POST"])
    @require("viewer")
    def categories() -> Any:
        if request.method == "POST":
            if ROLE_RANK[g.role] < 3:
                abort(403)
            key = request.form.get("key", "")
            db().execute("UPDATE categories SET enabled=?, auto_publish=? WHERE key=?",
                         (int(bool(request.form.get("enabled"))), int(bool(request.form.get("auto_publish"))), key))
            audit(db(), g.user, "category_edit", {"key": key})
            return redirect(url_for("categories"))
        return page("دسته‌بندی‌ها", view="categories", rows=db().execute("SELECT * FROM categories").fetchall())

    def _records_query() -> tuple[str, list[Any]]:
        where, args = ["1=1"], []
        for col, arg in (("status", "status"), ("category", "category")):
            if request.args.get(arg):
                where.append(f"r.{col}=?")
                args.append(request.args[arg])
        if request.args.get("source_id"):
            where.append("r.source_id=?")
            args.append(request.args.get("source_id", type=int))
        if request.args.get("q"):
            where.append("(r.asset_name LIKE ? OR r.snippet LIKE ? OR r.entity_key LIKE ?)")
            args += [f"%{request.args['q']}%"] * 3
        return ("SELECT r.*, s.name AS source_name FROM records r JOIN sources s ON s.id=r.source_id WHERE "
                + " AND ".join(where) + " ORDER BY r.id DESC"), args

    @app.route("/records")
    @require("viewer")
    def records() -> Any:
        sql, args = _records_query()
        rows, p = paged(sql, args)
        return page("رکوردها", view="records", rows=rows, page_no=p, flags_of=lambda r: [f["code"] for f in jload(r["validation"], [])])

    @app.route("/records.csv")
    @require("viewer")
    def records_csv() -> Any:
        sql, args = _records_query()
        rows = db().execute(sql + " LIMIT 5000", args).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["id", "observed_at", "source_name", "category", "asset_name", "side", "amount", "stated_unit",
                "amount_canonical", "canonical_unit", "status", "confidence", "dup_of", "dup_reason"]
        w.writerow(cols)
        for r in rows:
            # neutralise spreadsheet formula injection from untrusted text
            w.writerow([("'" + str(r[c]) if isinstance(r[c], str) and r[c][:1] in "=+-@" else r[c]) for c in cols])
        audit(db(), g.user, "export_csv", {"rows": len(rows)})
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=records.csv"})

    @app.route("/review")
    @require("viewer")
    def review() -> Any:
        rows, p = paged("SELECT r.*, s.name AS source_name FROM records r JOIN sources s ON s.id=r.source_id "
                        "WHERE r.status IN ('needs_review','invalid') ORDER BY r.id DESC", [])
        return page("صف بازبینی و خطاهای اعتبارسنجی", view="review", rows=rows, page_no=p,
                    flags_of=lambda r: [f["code"] for f in jload(r["validation"], [])])

    @app.route("/records/<int:rid>/review", methods=["POST"])
    @require("editor")
    def record_review(rid: int) -> Any:
        ok = pipeline.review_record(db(), rid, request.form.get("decision") == "approve", g.user,
                                    tzname=cfg.timezone)
        flash("انجام شد" if ok else "رکورد در وضعیت بازبینی نیست", "ok" if ok else "err")
        return redirect(request.referrer or url_for("review"))

    @app.route("/duplicates")
    @require("viewer")
    def duplicates() -> Any:
        rows, p = paged("SELECT r.*, s.name AS source_name, o.source_id AS orig_source, o.raw_id AS orig_raw "
                        "FROM records r JOIN sources s ON s.id=r.source_id LEFT JOIN records o ON o.id=r.dup_of "
                        "WHERE r.status='duplicate' ORDER BY r.id DESC", [])
        return page("تکراری‌ها", view="duplicates", rows=rows, page_no=p)

    @app.route("/opportunities")
    @require("viewer")
    def opps() -> Any:
        rows, p = paged("SELECT * FROM opportunities ORDER BY id DESC", [])
        return page("فرصت‌ها", view="opps", rows=rows, page_no=p, ev=lambda r: jload(r["evidence"], {}),
                    bd=lambda r: jload(r["score_breakdown"], []))

    @app.route("/opportunities/<int:oid>/decide", methods=["POST"])
    @require("editor")
    def opp_decide(oid: int) -> Any:
        pipeline.decide_opportunity(db(), oid, request.form.get("decision") == "approve", g.user, cfg.timezone)
        return redirect(url_for("opps"))

    @app.route("/publications")
    @require("viewer")
    def publications() -> Any:
        rows, p = paged("SELECT * FROM outbox ORDER BY id DESC", [])
        return page("صف انتشار و تاریخچه", view="outbox", rows=rows, page_no=p)

    @app.route("/publications/<int:oid>/<action>", methods=["POST"])
    @require("editor")
    def pub_action(oid: int, action: str) -> Any:
        d = db()
        with tx(d):
            if action == "approve":
                publisher.approve(d, oid, g.user)
            elif action == "cancel":
                publisher.cancel(d, oid, g.user)
            elif action == "retry":
                publisher.retry(d, oid, g.user, confirm_uncertain=bool(request.form.get("confirm_uncertain")))
            else:
                abort(404)
        return redirect(url_for("publications"))

    @app.route("/history")
    @require("viewer")
    def price_history() -> Any:
        key = request.args.get("entity_key", "")
        keys = [r[0] for r in db().execute("SELECT DISTINCT entity_key FROM records WHERE status IN ('valid','stale') "
                                           "ORDER BY entity_key LIMIT 300")]
        rows = analytics.history(db(), key, limit=200) if key else []
        pts = [(r["observed_at"], r["amount_canonical"]) for r in reversed(rows)]
        svg = _sparkline([p[1] for p in pts]) if len(pts) >= 2 else None
        return page("تاریخچه‌ی قیمت", view="history", keys=keys, key=key, rows=rows, svg=svg,
                    note=None if len(pts) >= 2 else "داده‌ی کافی برای نمایش روند وجود ندارد.")

    @app.route("/logs")
    @require("viewer")
    def logs() -> Any:
        lvl = request.args.get("level")
        rows, p = paged("SELECT * FROM event_log" + (" WHERE level=?" if lvl else "") + " ORDER BY id DESC",
                        [lvl] if lvl else [])
        return page("گزارش خطا و رویداد", view="logs", rows=rows, page_no=p)

    @app.route("/raw")
    @require("viewer")
    def raw_messages() -> Any:
        rows, p = paged("SELECT w.*, s.name AS source_name FROM raw_messages w JOIN sources s ON s.id=w.source_id "
                        "WHERE w.parse_status IN ('unparsed','partial','error') ORDER BY w.id DESC", [])
        return page("پیام‌های پارس‌نشده", view="raw", rows=rows, page_no=p)

    @app.route("/raw/<int:rid>/reprocess", methods=["POST"])
    @require("editor")
    def raw_reprocess(rid: int) -> Any:
        pipeline.reprocess_raw(db(), rid, g.user, tzname=cfg.timezone)
        return redirect(url_for("raw_messages"))

    @app.route("/templates", methods=["GET", "POST"])
    @require("viewer")
    def template_editor() -> Any:
        d = db()
        if request.method == "POST":
            if ROLE_RANK[g.role] < 2:
                abort(403)
            action = request.form.get("action", "save")
            if action == "save":
                brand = request.form.get("brand_line", "").strip()
                unit = request.form.get("display_unit", "toman").strip()
                thousands_sep = request.form.get("thousands_separator", ",").strip()
                show_source = 1 if request.form.get("show_source") else 0
                show_verification = 1 if request.form.get("show_verification") else 0
                show_source_url = 1 if request.form.get("show_source_url") else 0
                try:
                    alert_pct = float(request.form.get("alert_change_pct", "2.0"))
                except ValueError:
                    alert_pct = 2.0
                if brand:
                    set_setting(d, "brand_line", brand)
                if unit in ("toman", "rial"):
                    set_setting(d, "display_unit", unit)
                set_setting(d, "thousands_separator", thousands_sep)
                set_setting(d, "show_source", show_source)
                set_setting(d, "show_verification", show_verification)
                set_setting(d, "show_source_url", show_source_url)
                set_setting(d, "alert_change_pct", alert_pct)
                try:
                    interval_m = int(request.form.get("auto_bulletin_interval_minutes", "30"))
                except ValueError:
                    interval_m = 30
                set_setting(d, "auto_bulletin_interval_minutes", interval_m)
                flash("تنظیمات قالب با موفقیت ذخیره شد.", "ok")
                return redirect(url_for("template_editor"))
            elif action == "send_test":
                sample_text = request.form.get("sample_text", "").strip()
                target_chat = request.form.get("target_chat", "").strip() or cfg.publish_chat_id or "@nabzbazar360"
                if sample_text and cfg.bot_token:
                    try:
                        from ..senders import TelegramSender
                        sender = TelegramSender(cfg.bot_token)
                        msg_id = sender.send(target_chat, sample_text, [])
                        flash(f"پیام پیش‌نمایش با موفقیت به کانال ارسال شد (شناسه تلگرام: {msg_id})", "ok")
                    except Exception as exc:
                        flash(f"خطا در ارسال به تلگرام: {exc}", "err")
                else:
                    flash("متن پیام یا توکن ربات موجود نیست.", "err")
                return redirect(url_for("template_editor"))

        rec = d.execute("SELECT * FROM records WHERE amount_canonical IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        sample_quote = ""
        if rec:
            sample_quote, _ = publisher.render_record(d, rec, cfg.timezone, None, 1, "📊 <b>اعلام قیمت بازار</b>")
        else:
            sample_quote = "📊 <b>اعلام قیمت بازار</b>\n\n• دارایی: دلار آمریکا\n• قیمت: ۶۱,۲۰۰ تومان\n• زمان: ۱۴۰۵/۰۷/۲۴ - ۱۶:۰۰\n\n📈 نبض بازار ۳۶۰ | @nabzbazar360"

        settings_dict = {
            "brand_line": get_setting(d, "brand_line"),
            "display_unit": get_setting(d, "display_unit"),
            "alert_change_pct": get_setting(d, "alert_change_pct"),
            "thousands_separator": get_setting(d, "thousands_separator") or ",",
            "show_source": get_setting(d, "show_source"),
            "show_verification": get_setting(d, "show_verification"),
            "show_source_url": get_setting(d, "show_source_url"),
            "auto_bulletin_interval_minutes": get_setting(d, "auto_bulletin_interval_minutes") or 30,
            "publish_chat_id": cfg.publish_chat_id or "@nabzbazar360"
        }
        return page("قالب و پیش‌نمایش پیام‌ها", view="templates", sample_quote=sample_quote, cfg_settings=settings_dict)

    @app.route("/manual-publish", methods=["POST"])
    @require("editor")
    def manual_publish() -> Any:
        d = db()
        from ..adapters import telegram_web
        from ..publisher import render_car_bulletin, render_market_bulletin
        from ..senders import TelegramSender

        # 1. Fetch latest updates from web sources right now
        new_count = 0
        try:
            stats = telegram_web.poll_once(d, utcnow(), cfg.timezone)
            new_count = stats.get("new", 0)
        except Exception as exc:
            log.warning("manual poll error: %s", exc)

        # 2. Build the bulletin (either market or car)
        b_type = request.form.get("bulletin_type", "market")
        if b_type == "car":
            bulletin = render_car_bulletin(d, cfg.timezone)
            title_name = "نبض خودرو"
        else:
            bulletin = render_market_bulletin(d, cfg.timezone)
            title_name = "نبض بازار"

        # 3. Publish to Telegram channel
        target_chat = cfg.publish_chat_id or "@nabzbazar360"
        if not cfg.bot_token:
            flash("توکن ربات تلگرام در تنظیمات (.env) یافت نشد.", "err")
            return redirect(request.referrer or url_for("overview"))

        try:
            sender = TelegramSender(cfg.bot_token)
            msg_id = sender.send(target_chat, bulletin, [])
            audit(d, g.user, "manual_publish", {"chat": target_chat, "msg_id": msg_id, "type": b_type, "new_messages": new_count})
            flash(f"⚡ پایش فوری انجام شد ({new_count} پیام جدید پردازش شد) و پست {title_name} با موفقیت در کانال منتشر گردید (شناسه تلگرام: {msg_id})", "ok")
        except Exception as exc:
            flash(f"خطا در انتشار به کانال تلگرام: {exc}", "err")

        return redirect(request.referrer or url_for("overview"))

    @app.route("/settings", methods=["GET", "POST"])
    @require("viewer")
    def settings_page() -> Any:
        d = db()
        if request.method == "POST":
            if ROLE_RANK[g.role] < 3:
                abort(403)
            changed = {}
            for k, default in DEFAULT_SETTINGS.items():
                if k.startswith("_") or k not in request.form:
                    continue
                raw = request.form[k].strip()
                try:
                    if isinstance(default, bool) or k == "paused":
                        val: Any = 1 if raw in ("1", "true", "on") else 0
                    elif isinstance(default, int):
                        val = int(raw)
                    elif isinstance(default, float):
                        val = float(raw)
                    elif isinstance(default, (list, dict)):
                        val = json.loads(raw)
                    else:
                        val = raw
                except ValueError:
                    abort(400, f"invalid value for {k}")
                if k == "publish_mode" and val not in ("manual", "auto", "auto_categories"):
                    abort(400, "invalid publish_mode")
                if get_setting(d, k) != val:
                    set_setting(d, k, val)
                    changed[k] = val
            if changed:
                audit(d, g.user, "settings_change", changed)
            return redirect(url_for("settings_page"))
        s = {k: v for k, v in all_settings(d).items() if not k.startswith("_")}
        return page("تنظیمات", view="settings", s=s, json=json)

    @app.route("/emergency-pause", methods=["POST"])
    @require("editor")
    def emergency_pause() -> Any:
        new = 0 if request.form.get("resume") else 1
        set_setting(db(), "paused", new)
        audit(db(), g.user, "emergency_pause" if new else "resume", None)
        return redirect(url_for("overview"))

    return app


def _sparkline(vals: list[float], w: int = 640, h: int = 120) -> str:
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pts = " ".join(f"{i * (w - 10) / (len(vals) - 1) + 5:.1f},{h - 10 - (v - lo) / span * (h - 20):.1f}"
                   for i, v in enumerate(vals))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" aria-label="price history">'
            f'<polyline fill="none" stroke="#2563eb" stroke-width="2" points="{pts}"/></svg>')
