from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    db_path: str
    timezone: str
    log_level: str
    bot_token: str
    publish_chat_id: str
    live_publish: bool
    admin_secret_key: str
    admin_host: str
    admin_port: int
    cookie_secure: bool

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()
        g = os.environ.get
        return cls(
            db_path=g("MI_DB_PATH", "data/marketintel.sqlite3"),
            timezone=g("MI_TIMEZONE", "Asia/Tehran"),
            log_level=g("MI_LOG_LEVEL", "INFO"),
            bot_token=g("MI_TELEGRAM_BOT_TOKEN", ""),
            publish_chat_id=g("MI_PUBLISH_CHAT_ID", ""),
            live_publish=g("MI_LIVE_PUBLISH", "0") == "1",
            admin_secret_key=g("MI_ADMIN_SECRET_KEY", ""),
            admin_host=g("MI_ADMIN_HOST", "0.0.0.0" if g("PORT") else "127.0.0.1"),
            admin_port=int(g("PORT") or g("MI_ADMIN_PORT", "8080")),
            cookie_secure=g("MI_ADMIN_COOKIE_SECURE", "0") == "1",
        )
