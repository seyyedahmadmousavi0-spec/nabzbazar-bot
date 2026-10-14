# Persian Market Intelligence Platform (marketintel)

Collects prices/offers from **authorized** Telegram channels, normalizes Persian text, validates, deduplicates,
tracks history, detects opportunities and publishes attributed Persian posts to **your own** channel – with a
secure RTL admin dashboard. **Status: v0.1, working core verified with synthetic data and mocked Telegram.
Never yet run against real Telegram – see `docs/implementation-roadmap.md`.**

## Quick start (Windows PowerShell)
```powershell
cd path\to\tgmi
.\scripts\setup.ps1                       # venv + deps + .env + database
.\.venv\Scripts\python.exe -m marketintel demo            # synthetic end-to-end run, DRY-RUN publishing
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```
Linux/macOS: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && python -m marketintel demo`

## Dashboard
```powershell
# edit .env: set MI_ADMIN_SECRET_KEY to a long random value (the app refuses the placeholder)
.\.venv\Scripts\python.exe -m marketintel create-user admin --role admin     # prompts for password (min 10 chars)
.\.venv\Scripts\python.exe -m marketintel demo --keep                        # optional: fill MI_DB_PATH with synthetic data
.\.venv\Scripts\python.exe -m marketintel serve                              # http://127.0.0.1:8080
```
Binds to 127.0.0.1 by default. To expose it, put it behind HTTPS (reverse proxy), set `MI_ADMIN_COOKIE_SECURE=1`.
Roles: **viewer** (read), **editor** (review, approve/cancel/retry, pause), **admin** (+ settings, sources).

## Going live (checklist)
1. **Bot:** create with @BotFather → put token in `.env` (`MI_TELEGRAM_BOT_TOKEN`). Add the bot as **admin** to your publishing channel and to each source channel *whose owners authorize it*. A bot cannot join arbitrary channels or read history.
2. **Test first:** create a private test channel, set `MI_PUBLISH_CHAT_ID` to it and `MI_LIVE_PUBLISH=1`. Without `MI_LIVE_PUBLISH=1` nothing is sent (console dry-run).
3. **Sources:** copy `config/sources.example.json` → `config/sources.json`, fill in, `python -m marketintel load-sources config/sources.json`; or manage in the dashboard. Set `chat_ref` (`@username` or `-100…` id), `categories`, `reliability`, `attribution`, `restrictions`; set `default_unit` only if the source *always* omits units.
4. **Run:** `python -m marketintel worker` (one process; polls Bot API, processes, publishes). Stop with Ctrl+C (finishes current cycle).
5. **Mode:** keep `publish_mode=manual` until `needs_review`/`unparsed` rates look good; then `auto_categories`.
6. **Switch to the real channel** only after the test channel looks right.
Optional MTProto: `pip install telethon`, set `MI_TG_API_*`, source `access_method=mtproto` – *untested*; use only for channels your account may legitimately read.

## Commands
`init-db`, `create-user`, `load-sources`, `demo [--keep]`, `run-once`, `worker [--interval s]`, `serve`, `backup [--out file]`.

## Configuration
Environment (`.env`, see `.env.example`) for secrets/paths; runtime settings (thresholds, modes, quiet hours, weights) in the dashboard **Settings** page, stored in the DB and audited. Dictionaries: `marketintel/data/assets.json`, `cars.json` (override dir with `MI_DATA_DIR`).

## Backup / restore
```powershell
python -m marketintel backup                       # consistent online copy -> backups\marketintel-<time>.sqlite3
# restore: stop worker + dashboard, then
Copy-Item backups\marketintel-XXXX.sqlite3 data\marketintel.sqlite3 -Force; Remove-Item data\marketintel.sqlite3-wal,data\marketintel.sqlite3-shm -ErrorAction SilentlyContinue
```
Retention: per-source `retention_days` scrubs raw message text (normalized records stay); run hourly inside the worker loop (every 720 cycles).

## Deployment
Single process + dashboard process, e.g. Windows Task Scheduler / NSSM service, or systemd on Linux (`ExecStart=… -m marketintel worker`). Back up the SQLite file nightly. Docker intentionally not provided (not needed for this footprint).

## Troubleshooting
| Symptom | Check |
|---|---|
| Nothing ingested | Bot is admin in the source channel? `chat_ref` matches `@username`/numeric id? Dashboard → logs "updates from unconfigured chats ignored". Another webhook/`getUpdates` consumer on the same token? |
| Source shows `failing` | Dashboard → Sources → last error; network/token problem. Recovers automatically after the next successful poll. |
| Many `needs_review` | See flag codes in Records (`missing_unit`, `possible_scale_error`, `unconfirmed_jump`…); fix source `default_unit` only if justified. |
| `unparsed` messages | Dashboard → پارس‌نشده; extend dictionaries/parsers, then "reprocess". |
| Item stuck `uncertain` | Process crashed mid-send. Check the channel; if absent, retry (confirm) from Publications. |
| Posts not sent | Paused? quiet hours? `MI_LIVE_PUBLISH`? mode `manual` (needs approval)? |

## Safety & compliance
Uses only authorized access; no scraping, no credential tricks. Stores source attribution/URL and reuse restrictions per source (you are responsible for honoring them). Financial posts are informational, not advice; opportunity posts carry assumptions and a non-advice notice. Never commit `.env` or session files.

Docs: `docs/product-requirements.md`, `docs/architecture.md`, `docs/competitive-research.md`, `docs/implementation-roadmap.md`.
