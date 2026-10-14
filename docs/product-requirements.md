# Product requirements (condensed from the brief)

**Goal:** a Persian (RTL) Telegram channel publishing trustworthy, attributed market information collected from
authorized sources, with history, validation, deduplication, opportunity detection and operator control.

## Functional requirements and status
| # | Requirement | Status |
|---|---|---|
| R1 | Configurable sources (enable/disable, categories, parser, default unit, reliability, attribution, restrictions, retention, health) – no code change per source | **Done** (DB + JSON loader + dashboard) |
| R2 | Bot API ingestion incl. edits, captions, links, forwards | **Done**, tested against mocked Bot API |
| R3 | MTProto ingestion for permitted channels | Adapter written, **untested live** |
| R4 | External licensed price-provider adapter | **Planned** (interface point: `adapters/`) |
| R5 | Raw message kept separately from normalized records | **Done** |
| R6 | Persian/Arabic digits, ی/ک variants, separators, toman/rial, scale words, ranges, jalali dates/times | **Done** |
| R7 | Never guess units silently; record the rule | **Done** (`unit_rule`, `missing_unit` → review) |
| R8 | Category parsers: currency, gold, coin, crypto, car, deal; source-specific parser hook | **Done** (starter dictionaries, extendable JSON) |
| R9 | Optional AI extraction provider | **Planned** – not implemented; no paid API is required anywhere |
| R10 | Dedup: same message, same-source repeat, cross-source copy, forwards, car reposts, no merging of distinct variants | **Done** |
| R11 | Validation rules + explainable confidence; no false "verified" label | **Done** (heuristic, uncalibrated) |
| R12 | History, latest-by-source, change over window, stale detection, summaries, source statistics | **Done** |
| R13 | Opportunities with explanation, evidence, links, timestamps, assumptions, transparent configurable score | **Done**: spread, price drop, car below comparables, deal discount, limited offer |
| R14 | Publishing: templates, modes (manual / auto / auto-by-category / pause), quiet hours, rate control, outbox, safe retries, summaries, alerts | **Done**, tested with mock sender |
| R15 | Admin dashboard (RTL, auth, roles, CSRF, audit, CSV, retry, thresholds) | **Done** |
| R16 | Health endpoint, structured logs w/ correlation ids and secret redaction, watchdog, backups, retention | **Done** |
| R17 | Real-channel verification with real credentials | **Not possible here** – needs your token/channels |
| R18 | Docker | Not provided (not justified for a single-process SQLite deployment) |

## Non-goals (for now)
Investment advice, predictions, scraping of third-party sites, multi-tenant SaaS billing.
