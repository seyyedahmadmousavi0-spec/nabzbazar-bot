CREATE TABLE sources (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  source_type TEXT NOT NULL,            -- telegram_channel | external_api | fixture
  access_method TEXT NOT NULL,          -- bot_api | mtproto | http_json | fixture
  enabled INTEGER NOT NULL DEFAULT 1,
  categories TEXT NOT NULL DEFAULT '[]',-- JSON list
  parser TEXT NOT NULL DEFAULT 'generic',
  default_unit TEXT,                    -- IRT/IRR applied ONLY when configured (recorded as rule)
  reliability REAL NOT NULL DEFAULT 0.7,
  chat_ref TEXT,                        -- Telegram chat id or @username for matching updates
  attribution TEXT,                     -- display name / attribution text
  url_template TEXT,                    -- e.g. https://t.me/{username}/{message_id}
  restrictions TEXT,                    -- free-text reuse restrictions agreed with the source
  retention_days INTEGER NOT NULL DEFAULT 365,
  rate_limit_per_min INTEGER NOT NULL DEFAULT 20,
  poll_interval_s INTEGER NOT NULL DEFAULT 30,
  config TEXT NOT NULL DEFAULT '{}',
  cursor TEXT,
  health TEXT NOT NULL DEFAULT 'unknown',   -- unknown | ok | degraded | failing
  last_success_at TEXT,
  last_error TEXT,
  last_error_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE categories (
  key TEXT PRIMARY KEY,
  name_fa TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  auto_publish INTEGER NOT NULL DEFAULT 0,
  stale_minutes INTEGER NOT NULL DEFAULT 360
);
CREATE TABLE raw_messages (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  external_id TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  norm_text TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  raw_json TEXT,
  message_date TEXT NOT NULL,
  received_at TEXT NOT NULL,
  url TEXT,
  forward_from TEXT,
  is_current INTEGER NOT NULL DEFAULT 1,
  deleted_at TEXT,
  parse_status TEXT NOT NULL DEFAULT 'pending', -- pending|parsed|partial|unparsed|error
  parse_note TEXT,
  UNIQUE(source_id, external_id, revision)
);
CREATE INDEX idx_raw_hash ON raw_messages(text_hash);
CREATE INDEX idx_raw_status ON raw_messages(parse_status);
CREATE TABLE records (
  id INTEGER PRIMARY KEY,
  raw_id INTEGER NOT NULL REFERENCES raw_messages(id),
  source_id INTEGER NOT NULL REFERENCES sources(id),
  category TEXT NOT NULL,
  asset_key TEXT,
  asset_name TEXT,
  brand TEXT, model TEXT, variant TEXT, model_year INTEGER,
  side TEXT,                            -- buy | sell | NULL
  amount REAL,                          -- as stated (after explicit scale word)
  price_low REAL, price_high REAL,      -- set for ranges
  stated_unit TEXT,                     -- IRT | IRR | USD | NULL
  unit_rule TEXT,                       -- how the unit was determined (audit)
  amount_canonical REAL,                -- IRR for IRT/IRR, USD for USD
  canonical_unit TEXT,
  unit_of_measure TEXT,
  price_type TEXT NOT NULL DEFAULT 'quoted_market',
  location TEXT,
  observed_at TEXT NOT NULL,
  observed_at_source TEXT NOT NULL DEFAULT 'message_date', -- message_date | stated_time
  published_at TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  extras TEXT NOT NULL DEFAULT '{}',
  snippet TEXT,
  parse_method TEXT,
  confidence REAL NOT NULL DEFAULT 0,
  confidence_detail TEXT NOT NULL DEFAULT '[]',
  validation TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,                 -- valid|needs_review|invalid|duplicate|stale|superseded|retracted|rejected
  dup_of INTEGER REFERENCES records(id),
  dup_reason TEXT,
  reviewed_by TEXT,
  reviewed_at TEXT
);
CREATE INDEX idx_rec_entity_time ON records(entity_key, observed_at);
CREATE INDEX idx_rec_status ON records(status);
CREATE INDEX idx_rec_cat ON records(category, observed_at);
CREATE INDEX idx_rec_source ON records(source_id, observed_at);
CREATE TABLE opportunities (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  category TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  title TEXT NOT NULL,
  explanation TEXT NOT NULL,
  assumptions TEXT NOT NULL,
  evidence TEXT NOT NULL,               -- JSON: observations, urls, timestamps
  score REAL NOT NULL,
  score_breakdown TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected|published|expired
  dedupe_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  decided_by TEXT,
  decided_at TEXT
);
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                   -- alert|summary|opportunity
  category TEXT NOT NULL,
  ref_type TEXT, ref_id INTEGER,
  text TEXT NOT NULL,
  buttons TEXT NOT NULL DEFAULT '[]',   -- JSON [[{text,url}]]
  idempotency_key TEXT NOT NULL UNIQUE,
  record_ids TEXT NOT NULL DEFAULT '[]',
  confidence REAL,
  status TEXT NOT NULL,                 -- pending_approval|queued|sending|sent|failed|cancelled|uncertain
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  lease_until TEXT,
  last_error TEXT,
  telegram_message_id TEXT,
  target_chat TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT,
  approved_by TEXT
);
CREATE INDEX idx_outbox_status ON outbox(status, next_attempt_at);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','editor','viewer')),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT
);
CREATE TABLE event_log (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, level TEXT NOT NULL, component TEXT NOT NULL,
  message TEXT NOT NULL, correlation_id TEXT, context TEXT
);
CREATE INDEX idx_event_level ON event_log(level, ts);
