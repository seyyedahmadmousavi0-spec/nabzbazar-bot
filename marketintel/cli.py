from __future__ import annotations

import argparse
import getpass
import json
import logging
import sqlite3
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

from . import __version__, db
from .adapters import bot_api
from .config import Config
from .logging_setup import setup_logging
from .runner import build_sender, run_cycle, worker
from .senders import ConsoleSender
from .sources import load_categories, load_sources_file
from .catalog import default_catalog
from .util import iso, utcnow


def _conn(cfg: Config) -> sqlite3.Connection:
    c = db.connect(cfg.db_path)
    db.migrate(c)
    try:
        if not c.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
            load_categories(c, default_catalog().categories)
        sources_file = Path("config/sources.json")
        if sources_file.exists():
            load_sources_file(c, sources_file)
        if not c.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
            import os
            admin_pass = os.environ.get("MI_ADMIN_PASSWORD", "admin123456")
            c.execute("INSERT OR IGNORE INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?)",
                      ("admin", generate_password_hash(admin_pass), "admin", iso(utcnow())))
        if not c.execute("SELECT 1 FROM settings WHERE key='auto_bulletin_interval_minutes'").fetchone():
            db.set_setting(c, "auto_bulletin_interval_minutes", 15)
            db.set_setting(c, "publish_mode", "auto")
            db.set_setting(c, "show_source", 0)
            db.set_setting(c, "show_verification", 0)
            db.set_setting(c, "show_source_url", 0)
            db.set_setting(c, "thousands_separator", ",")
            db.set_setting(c, "brand_line", "📈 نبض بازار | @nabzbazar360")
            db.set_setting(c, "auto_categories", ["currency", "gold", "coin", "crypto", "car"])
        if not c.execute("SELECT 1 FROM settings WHERE key='auto_car_bulletin_interval_hours'").fetchone():
            db.set_setting(c, "auto_car_bulletin_interval_hours", 6)
    except Exception as exc:
        logging.getLogger(__name__).warning("auto-bootstrap warning: %s", exc)
    return c


def cmd_init_db(cfg: Config, a: argparse.Namespace) -> int:
    c = _conn(cfg)
    load_categories(c, default_catalog().categories)
    print(f"database ready at {cfg.db_path}")
    return 0


def cmd_create_user(cfg: Config, a: argparse.Namespace) -> int:
    c = _conn(cfg)
    pw = a.password or getpass.getpass("password (min 10 chars): ")
    if len(pw) < 10:
        print("password too short", file=sys.stderr)
        return 2
    c.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES (?,?,?,?) ON CONFLICT(username) DO "
              "UPDATE SET password_hash=excluded.password_hash, role=excluded.role, active=1",
              (a.username, generate_password_hash(pw), a.role, iso(utcnow())))
    print(f"user {a.username} ({a.role}) saved")
    return 0


def cmd_load_sources(cfg: Config, a: argparse.Namespace) -> int:
    c = _conn(cfg)
    load_categories(c, default_catalog().categories)
    print("loaded:", ", ".join(load_sources_file(c, a.file)))
    return 0


def cmd_demo(cfg: Config, a: argparse.Namespace) -> int:
    from .demo import demo_updates, seed_sources
    c = db.connect(a.db or ":memory:" if not a.keep else cfg.db_path)
    db.migrate(c)
    seed_sources(c)
    db.set_setting(c, "publish_mode", "auto_categories")
    db.set_setting(c, "auto_categories", ["currency"])
    db.set_setting(c, "min_auto_confidence", 0.6)
    db.set_setting(c, "min_interval_seconds", 0)
    now = utcnow()
    client = bot_api.FixtureBotClient(demo_updates(now))
    sender = ConsoleSender()
    print(json.dumps(run_cycle(c, cfg, client, sender, now), ensure_ascii=False))
    for t in ("raw_messages", "records", "opportunities", "outbox"):
        print(t, c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    print("records by status:", dict(c.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall()))
    print("outbox by status:", dict(c.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status").fetchall()))
    for _, text in sender.sent[:1]:
        print("--- sample published message (DRY RUN) ---\n" + text)
    if a.keep:
        print(f"demo data kept in {cfg.db_path}; start the dashboard with: python -m marketintel serve")
    return 0


def cmd_run_once(cfg: Config, a: argparse.Namespace) -> int:
    c = _conn(cfg)
    client = bot_api.HttpBotClient(cfg.bot_token) if cfg.bot_token else None
    print(json.dumps(run_cycle(c, cfg, client, build_sender(cfg)), ensure_ascii=False))
    return 0


def cmd_worker(cfg: Config, a: argparse.Namespace) -> int:
    c = _conn(cfg)
    client = bot_api.HttpBotClient(cfg.bot_token) if cfg.bot_token else None
    if not cfg.live_publish:
        logging.getLogger(__name__).warning("MI_LIVE_PUBLISH!=1: publishing is DRY-RUN (console only)")
    worker(c, cfg, client, build_sender(cfg), interval=a.interval)
    return 0


def cmd_run_all(cfg: Config, a: argparse.Namespace) -> int:
    import os
    import threading
    print(f"=== Starting MarketIntel on {cfg.admin_host}:{cfg.admin_port} ===", flush=True)
    c = _conn(cfg)
    client = bot_api.HttpBotClient(cfg.bot_token) if cfg.bot_token else None
    sender = build_sender(cfg)
    interval = getattr(a, "interval", 20.0)
    stop = threading.Event()
    worker_thread = threading.Thread(
        target=worker,
        args=(c, cfg, client, sender),
        kwargs={"interval": interval, "stop": stop},
        daemon=True,
        name="WorkerThread"
    )
    worker_thread.start()
    logging.getLogger(__name__).info("Background worker started alongside web dashboard (interval=%.1fs)", interval)

    from .admin.app import create_app
    app = create_app(cfg)
    try:
        app.run(host=cfg.admin_host, port=cfg.admin_port, debug=False, threaded=True)
    finally:
        stop.set()
    return 0


def cmd_serve(cfg: Config, a: argparse.Namespace) -> int:
    import os
    if getattr(a, "with_worker", False) or os.environ.get("MI_RUN_WORKER") == "1":
        return cmd_run_all(cfg, a)
    from .admin.app import create_app
    app = create_app(cfg)
    app.run(host=cfg.admin_host, port=cfg.admin_port, debug=False, threaded=True)
    return 0


def cmd_backup(cfg: Config, a: argparse.Namespace) -> int:
    c = _conn(cfg)
    out = Path(a.out or f"backups/marketintel-{utcnow():%Y%m%d-%H%M%S}.sqlite3")
    out.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(out))
    c.backup(dst)  # consistent online backup (safe while the worker runs)
    dst.close()
    print(f"backup written: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="marketintel", description=f"Market intelligence platform {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    u = sub.add_parser("create-user")
    u.add_argument("username")
    u.add_argument("--role", choices=["admin", "editor", "viewer"], default="admin")
    u.add_argument("--password")
    u.set_defaults(fn=cmd_create_user)
    ls = sub.add_parser("load-sources")
    ls.add_argument("file")
    ls.set_defaults(fn=cmd_load_sources)
    d = sub.add_parser("demo", help="end-to-end run on SYNTHETIC data (dry-run publishing)")
    d.add_argument("--keep", action="store_true", help="write into MI_DB_PATH instead of memory")
    d.add_argument("--db")
    d.set_defaults(fn=cmd_demo)
    sub.add_parser("run-once").set_defaults(fn=cmd_run_once)
    w = sub.add_parser("worker")
    w.add_argument("--interval", type=float, default=5.0)
    w.set_defaults(fn=cmd_worker)
    s = sub.add_parser("serve")
    s.add_argument("--with-worker", action="store_true", help="run background worker thread inside web process")
    s.add_argument("--interval", type=float, default=20.0)
    s.set_defaults(fn=cmd_serve)
    ra = sub.add_parser("run-all", help="run both web dashboard and background worker in a single process (ideal for cloud/docker)")
    ra.add_argument("--interval", type=float, default=20.0)
    ra.set_defaults(fn=cmd_run_all)
    b = sub.add_parser("backup")
    b.add_argument("--out")
    b.set_defaults(fn=cmd_backup)
    a = p.parse_args(argv)
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    return int(a.fn(cfg, a))
