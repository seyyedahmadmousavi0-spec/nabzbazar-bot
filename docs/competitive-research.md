# Competitive & technical research

> **Honesty note.** This research pass was short and partly limited by search quality. Only items
> I actually saw in search results are listed; anything not verified is marked *(not verified)*.
> No licences, feature lists or activity levels were verified for third-party projects – **do not reuse
> third-party code before checking its licence**. A deeper pass is listed in `implementation-roadmap.md`.

## 1. Telegram access methods (primary source: official Bot API documentation, core.telegram.org/bots/api)

| Fact used in the design | Consequence |
|---|---|
| Bot API `getUpdates` is long polling; `allowed_updates` selects update types (`channel_post`, `edited_channel_post`). | Ingestion adapter requests exactly those two types. |
| A bot only receives posts of channels it belongs to (as admin); there is no history endpoint. | No backfill via Bot API; sources must add the bot, or use MTProto with an authorized account. |
| Channel post deletions are not delivered as updates. | Deletions are only observable through other mechanisms (e.g. MTProto); `mark_deleted` exists for those. |
| Errors with HTTP 429 carry `parameters.retry_after`. | `TelegramSender` raises `RateLimited(retry_after)`; the dispatcher postpones all sending. |
| `getUpdates` cannot be combined with an active webhook; one consumer per bot. | One polling process per bot token (documented in README). |
| `sendMessage` has no idempotency key. | The outbox uses an `idempotency_key` locally and marks crash-ambiguous sends as `uncertain` instead of re-sending. |
| Message text limit 4096 characters. | Renderers cap at 4000 characters. |

MTProto / Telethon: search results only returned weak matches (a FloodWait helper package page and
unrelated Rust crates). Telethon is therefore treated as **optional and unverified**: the adapter in
`adapters/mtproto.py` is written against Telethon's documented sync API surface from prior knowledge, its mapping
function is unit-tested with fake objects, **and it has not been run against real Telegram**.

## 2. Iranian market products seen in search results (existence only)

| Item | What was visible | Not verified |
|---|---|---|
| Bonbast (bonbast.com) and a PyPI package `bonbast` 0.1.3 | A site/webmaster page and an unofficial client package exist. | Terms of use, licence, API stability, whether scraping is permitted. |
| `tgju-api-go` (pkg.go.dev, amiranmanesh) | An unofficial Go client for TGJU data exists. | Licence, maintenance, whether TGJU permits this use. |
| "Iran Rial Market API" (oanor.com) | A third-party API listing exists. | Pricing, data provenance, reliability. |

Decision: **no scraping of these sites is implemented.** The platform consumes (a) your authorized Telegram
sources and (b) licensed/official feeds through a provider adapter interface (planned, see roadmap). Any
provider you choose must be checked for terms of use first.

## 3. Categories with no research yet *(not researched in this session)*
Automotive classifieds (e.g. Divar/Bama), coupon/deal platforms, entity-resolution libraries, and dashboard
products were **not** investigated. Design choices for those areas come from general engineering practice, not from
cited products: token-set (Jaccard) similarity for syndication detection, median/MAD for outliers, transactional
outbox for delivery, role-based access with CSRF for the admin UI.

## 4. Adopt / adapt / avoid
* **Adopt:** stdlib `sqlite3` + SQL migrations, Flask + Jinja (already available, tiny), `requests` for the Bot API
  (no heavy bot framework needed: we only call `getUpdates` and `sendMessage`).
* **Adapt:** Telethon as an optional second ingestion path.
* **Avoid:** scraping price websites, mandatory paid AI APIs, Redis/Celery (workload does not need them).
