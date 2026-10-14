# Architecture

Modular monolith, Python 3.11+, SQLite (WAL) via stdlib `sqlite3`, Flask admin, `requests` for the Bot API.
Chosen because the workload (tens of sources, thousands of messages/day) does not justify PostgreSQL/Redis/Celery;
module boundaries below allow moving to PostgreSQL later (SQL is plain and isolated in a few modules).

```
 Telegram Bot API ─┐                          ┌─> analytics ─┐
 MTProto (opt.)  ──┼─> adapters ─> ingest_raw ─> parsing ─> validation ─> dedupe ─> records
 Provider feeds* ──┘   (raw_messages)                                          │
                                                                               ├─> opportunities (pending → approve)
                                                                               └─> publisher.outbox ─> sender ─> channel
 Flask admin (auth, roles, CSRF, audit) reads/controls everything; worker loop runs run_cycle()
```
`*` planned.

| Concern | Module |
|---|---|
| Telegram ingestion adapters | `adapters/bot_api.py`, `adapters/mtproto.py` |
| Raw storage, edits, deletions | `pipeline.py` (`ingest_raw`, `mark_deleted`) |
| Parsing / normalization | `textnorm.py`, `parsing.py`, `catalog.py`, `data/*.json`, `jalali.py` |
| Validation & confidence | `validation.py` |
| Dedup / entity resolution | `dedupe.py` (+ `entity_key` in `parsing.py`) |
| History & analytics | `analytics.py` |
| Opportunities | `opportunities.py` |
| Publishing & delivery | `publisher.py`, `senders.py` |
| Admin | `admin/app.py`, `admin/templates` |
| Config / secrets | `config.py` (env + `.env`), `db.py` (runtime settings) |
| Monitoring | `logging_setup.py`, `runner.py` (watchdog), `/healthz`, `event_log` |

## Key design decisions
* **Provenance:** every record → `raw_messages` row (original text, URL, revision) → source. Outbox rows keep `record_ids`; opportunities keep evidence with links and timestamps.
* **Units:** `stated_unit` + `unit_rule` are stored; canonical amount is IRR (toman×10) or USD. Units come from, in order: price token → message header ("قیمت‌ها به تومان") → *configured* source default (flagged, lower confidence) → none (`missing_unit` → human review, canonical amount stays NULL).
* **Entities:** `entity_key` = category + asset/brand/model/variant/year/location + side + measure. Different keys are never merged or compared.
* **Verification wording:** a record is described as corroborated only when ≥1 *other source* has an independent, non-duplicate, fresh observation within tolerance; otherwise "single source, unconfirmed".
* **Jumps:** large moves versus 24h history are `needs_review` unless another source corroborates; scale errors (×/÷10ⁿ) and out-of-bounds values are separate flags. Two sources reporting the same large move release each other.
* **Confidence:** reliability × parse-method factor − named penalties; stored with its breakdown; explicitly *not* a probability.
* **Outbox / delivery:** `UNIQUE(idempotency_key)`; claim = atomic status change with lease; success stores Telegram `message_id`. A crash after claiming but before recording leaves `sending`; after lease expiry it becomes `uncertain` and is **never auto-resent** (Bot API has no idempotency key, so auto-resend could double-post). An operator checks the channel and retries explicitly.
* **Pause:** `paused=1` stops *all* sends (manual-approved included) – the emergency brake.
* **Opportunities** always require human approval, even in auto modes.
* **Time:** all timestamps UTC ISO-8601; rendered in `Asia/Tehran` with Jalali dates.

## Known limitations
* Bot API: no history, no deletion events, one consumer per bot.
* Same-text short messages from two channels cannot be told apart from copies unless forwarded; they are kept as independent observations (conservative; may over-count corroboration).
* Parsers are deterministic and dictionary-based; unseen templates → `unparsed` (visible in dashboard, reprocess after dictionary fix).
* Dictionary bounds in `assets.json` are wide sanity limits, not market data; tune them.
* SQLite: single writer; run one worker process.
