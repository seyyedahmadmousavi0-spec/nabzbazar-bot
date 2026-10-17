"""One processing cycle + long-running worker with graceful shutdown."""
from __future__ import annotations

import logging
import signal
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any

from . import opportunities, publisher
from .adapters import bot_api, telegram_web
from .config import Config
from .db import log_event, tx
from .senders import ConsoleSender, Sender, TelegramSender
from .sources import enabled_sources
from .util import iso, utcnow

log = logging.getLogger(__name__)


def build_sender(cfg: Config) -> Sender:
    if cfg.live_publish and cfg.bot_token and cfg.publish_chat_id:
        return TelegramSender(cfg.bot_token)
    return ConsoleSender()


def purge_retention(conn: sqlite3.Connection, now: datetime) -> int:
    """Scrub raw text of messages older than each source's retention window (normalized records stay)."""
    n = 0
    for s in conn.execute("SELECT id, retention_days FROM sources").fetchall():
        cutoff = iso(now - timedelta(days=int(s["retention_days"])))
        n += conn.execute("UPDATE raw_messages SET text='[purged]', norm_text='', raw_json=NULL "
                          "WHERE source_id=? AND received_at<? AND text<>'[purged]'", (s["id"], cutoff)).rowcount
    return n


def run_cycle(conn: sqlite3.Connection, cfg: Config, client: bot_api.BotClient | None, sender: Sender,
              now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    out: dict[str, Any] = {}
    if client is not None:
        try:
            out["ingest"] = bot_api.poll_once(conn, client, now, cfg.timezone)
        except ConnectionError as exc:
            out["ingest_error"] = str(exc)
            log.error("ingestion failed: %s", exc)
    try:
        web_stats = telegram_web.poll_once(conn, now, cfg.timezone)
        if web_stats.get("sources", 0) > 0:
            out["web_ingest"] = web_stats
    except Exception as exc:
        out["web_ingest_error"] = str(exc)
        log.error("telegram_web ingestion failed: %s", exc)
    with tx(conn):
        from .db import get_setting
        if get_setting(conn, "enable_summaries"):
            publisher.schedule_summaries(conn, now, cfg.timezone)
        publisher.schedule_bulletin(conn, now, cfg.timezone)
        publisher.schedule_car_bulletin(conn, now, cfg.timezone)
        opportunities.expire_old(conn, now)
    out["publish"] = publisher.dispatch_due(conn, sender, cfg.publish_chat_id, now, cfg.timezone)
    return out


def source_watchdog(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Make silent stalls visible: enabled sources without success for 3x poll interval + 10 min."""
    stale = []
    for s in enabled_sources(conn):
        last = s["last_success_at"]
        if last is None:
            continue
        from .util import parse_iso
        if (now - parse_iso(last)).total_seconds() > 3 * s["poll_interval_s"] + 600:
            conn.execute("UPDATE sources SET health='failing' WHERE id=?", (s["id"],))
            log_event(conn, "ERROR", "watchdog", f"source {s['key']} has no successful sync since {last}")
            stale.append(s["key"])
    return stale


def worker(conn: sqlite3.Connection, cfg: Config, client: bot_api.BotClient | None, sender: Sender,
           interval: float = 5.0, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()

    def _handler(signum: int, _frame: Any) -> None:
        log.info("signal %s received, finishing current cycle then exiting", signum)
        stop.set()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except ValueError:  # not in main thread
                pass
    log.info("worker started (interval %.1fs)", interval)
    n = 0
    while not stop.is_set():
        try:
            run_cycle(conn, cfg, client, sender)
            source_watchdog(conn, utcnow())
            n += 1
            if n % 720 == 0:
                purge_retention(conn, utcnow())
        except Exception:
            log.exception("cycle failed")
        stop.wait(interval)
    log.info("worker stopped")
