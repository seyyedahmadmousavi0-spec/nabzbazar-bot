# Implementation checklist & roadmap

## Done (verified by `python -m unittest discover -s tests -t .` → 113 tests OK)
- [x] Project scaffold, `.env.example`, `.gitignore`, pinned `requirements.txt`, `pyproject.toml`
- [x] SQL migrations + runner, settings table, audit & event logs
- [x] Persian/Arabic normalization, numbers, units, scale words, Jalali ↔ Gregorian
- [x] Parsers: market lines (incl. buy/sell, ranges, multi-asset lines), cars, deals, `pipe_table` example, parser registry
- [x] Validation rules, explainable confidence, corroboration, scale-error and jump handling
- [x] Dedup (same message, repeat, syndicated, forwarded, car repost)
- [x] History/analytics, source statistics
- [x] Opportunity engine + transparent weighted score
- [x] Outbox, approval modes, pause, quiet hours, pacing, retry/backoff, rate limit, uncertain state
- [x] Bot API ingestion (mock-tested), Telegram sender (mock-tested), MTProto mapping (fake-object tested)
- [x] Admin dashboard (auth, roles, CSRF, audit, CSV, retries, settings, history chart)
- [x] CLI: init-db, create-user, load-sources, demo, run-once, worker, serve, backup
- [x] Local run exercised: init-db → demo → serve → login → all pages HTTP 200

## NOT verified / not done (be explicit)
- [ ] **Real Telegram**: never run with a real token/channel. First live test must use a private test channel.
- [ ] **Telethon/MTProto live run**; Telethon not installed here.
- [ ] `ruff` / `mypy`: configured in `pyproject.toml` but **not run** (no network to install). Only `compileall` and a crude unused-import scan were run.
- [ ] Windows: `scripts/setup.ps1` written, **not executed** (tests ran on Linux).
- [ ] Provider adapter for licensed price feeds; AI extraction provider (optional) – interfaces only planned.
- [ ] Deeper competitive research (cars, deals, entity resolution, dashboards).
- [ ] Backup *restore* drill (backup command tested manually only via code path; restore is a file copy – documented).
- [ ] Load/soak testing; PostgreSQL option; CSV import of sources via dashboard.

## Next steps (in order)
1. Create a bot + private test channel; run `worker` with `MI_LIVE_PUBLISH=1` against the test channel only.
2. Supply the ~10 source channels (handle/id, categories, message samples, agreed reuse restrictions) → create `config/sources.json`.
3. Collect real message samples (with permission) → extend `data/assets.json`, `data/cars.json`, add source-specific parsers, add them as regression fixtures.
4. Tune thresholds/bounds in Settings; keep `manual` mode until `unparsed`/`needs_review` rates are acceptable.
5. Install `ruff`, `mypy`; fix findings.
6. Decide on external licensed feeds; implement provider adapter.
