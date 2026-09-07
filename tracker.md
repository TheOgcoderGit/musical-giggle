# ChannelFlow AI Implementation Tracker

## Project

- Repository: https://github.com/TheOgcoderGit/musical-giggle
  - Local branch: `arena/01a07adf-musical-giggle` (work tree rooted at repo root; project source lives in `ChannelFlow_Bot/`)
  - Upstream `main` contains only: `ChannelFlow_AI_Master_PRD.md`, `ChannelFlow_Bot.zip` (base, 299,994 bytes), `README.md`
- Base ZIP: `ChannelFlow_Bot.zip` in repo root (the 2026-09-05 upload; the only base provided — no FIXED_v4 or older duplicate was used)
- PRD: `ChannelFlow_AI_Master_PRD.md` (3452 lines, now extended to §94 with UX-NAV-01/02/03–06)
- Current implementation batch: **Batch 1** (in progress → completed by this checkpoint)
- Last completed batch: (none — first batch on this tracker)
- Last tested batch: Batch 1 (tests executed below; results in Batch History)
- Next starting point: **Batch 2, Feature 1 = UX-NAV-01 (Unified Onboarding & Navigation UX)** — see Resume Point

## Status Legend

- COMPLETE = implemented and tested
- PARTIAL = partially implemented
- TODO = not implemented
- BUG = implemented but broken
- BLOCKED = blocked by external dependency/configuration
- UNTESTED = implementation exists but testing is incomplete
- VERIFIED = implementation + integration + runtime/test verification passed

## Audit Summary (2026-09-07, first full pass)

### What the base codebase contains (verified by inspection)

- Stack: python-telegram-bot 22.3 + Telethon 1.41 + SQLite (WAL), entry `python main.py` (also `pairing_server.py`).
- Bot layer: `bot/handlers.py` (~4.2k lines after Batch-1 edits; single router `button_handler` + `menu_handler`), `bot/keyboards.py`, `bot/states.py` (per-user waiting dicts), `bot/admin_panel.py` (adm:* panel), `bot/admin_promo_handlers.py` (/post,/broadcast), `bot/owner_panel.py`.
- Core: `core/user_sessions.py` (per-user /connect login), `core/session_crypto.py` (Fernet), `core/client_pool.py` (per-owner forwarding clients), `core/forwarder.py` (pipeline: filters → quota → text-replace → affiliate → AI → formatting → watermark → dedup → send/queue), `core/listener.py` (background tasks incl. queue worker + job handlers for notification/publish), `core/subscription_scheduler.py`, `core/pairing_endpoint.py`, `core/processing_listener.py`, `core/telegram_utils.py`.
- Services: ai, affiliate, content_rules, coupon, crypto_provider, dedup, destination, formatting, giveaway, i18n_service, instagram, job_queue, notification, payment, plan, platform_accounts, platform_registry, pricing, processing, promo, project, referral, settings, source, source_health, stats, support_ai, support, text_replacement, wallet, watermark, whatsapp_content, whatsapp_pairing.
- Destinations: telegram, whatsapp_channel (Cloud-API based, account-registry driven), threads, instagram (legacy flow gated behind UI "not live").
- DB: single `database/db.py` with `init_db()` = CREATE TABLE IF NOT EXISTS + `_migrate_existing_schema` (column adds) + `_migrate_late_tables`; ~70 tables incl. plan_configs, payment_requests, wallet_transactions, referrals, support_tickets, etc.

### Critical bugs found in the base (fixed in Batch 1)

1. **`core/user_sessions.py` — `_time.monotonic()` NameError** (PRD §5.8 exact bug): `submit_code`/`submit_password` timeout check referenced a nonexistent `_time` → any valid OTP crashed BEFORE reaching Telegram → "stuck after code" and OTP/attempt state never progressed. Fixed to `time.monotonic()`; same check also correctly routes to expiry text.
2. **No `/mycode` support**: no command registration, no handler; OTP text route expected bare text (or old `/myflow` prefix which was also unhandled). PRD §5.2/§76 mandatory command missing. Implemented `/mycode` + `/mycode 94563` + legacy `/myflow…`; bare numeric messages are never login codes (§5.3); `WAITING_CONNECT_STAGE` can no longer swallow plain text.
3. **No `get_me()` / external-account ownership**: sessions were keyed by ChannelFlow user id only (PRD §6.1/6.2 violated); two users could connect the same Telegram account; `telegram_id` PK meant "same ChannelFlow user" identity confusion. Implemented: capture real external id on login, store in `user_telegram_sessions.external_telegram_id`, partial unique index `idx_uts_one_active_owner` (active rows only), transactional claim with safe cross-user conflict error (PRD §6.3–6.7).
4. **`start()` /start crash for connected users**: `i18n.t(user_id, …)` referenced an undefined `user_id` (NameError). Fixed → `user.id`. (Relevant for UX-NAV-01 idempotent /start.)
5. **`WAITING_CONNECT_STAGE` import missing in user_sessions cleanup task** + values were strings but cleanup compared to monotonic times; cleanup task also never started from listener. Reworked: one cleanup task in `core/user_sessions` with timestamped flags (`WAITING_CONNECT_STAGE_STARTED`), started from `core/listener` via `start_cleanup_task` (single task now), stale phone/stage flags expire in lock-step with pending clients (PRD §43/44/45).
6. **No `users.trial_granted_at` marker**: old `start_trial` guard allowed trial re-grant after expiry/restart loopholes; trials only started post-connect (never at /start) (PRD §2.4, §82.1). Added marker column + rewritten idempotent/persistent `start_trial`; `/start` now grants the one-time trial.
7. **`core/forwarder.py _dispatch` dead-code indentation**: `owner_id = route.get("owner_id")` was indented under the rejected-filter early return → `UnboundLocalError` on EVERY real project message; quota reserve executed at the wrong place; second `reserve_daily_forward` call double-reserved and skipped the first reservation on success (PRD §22.1). Fixed to single atomic reservation right after filters. Proven crash before fix via runtime repro (`UnboundLocalError`), passes after.
8. **`test_final_fix.py` asserts per-forward wallet debiting** (`reserve_forward_charge`), contradicting the PRD no-per-forward-billing rule. Forwarder itself never called it; legacy API no-ops exist but the stale test remained → test updated to assert zero per-forward debit.
9. **Engine silently dropped invalid stored sessions** after restart (PRD §7.3): `client_pool` only logged + returned. Now marks `needs_reconnect` so UI can offer Reconnect.
10. **`wallet_service` has legacy `charge_forward/reserve/confirm/release_forward_charge` no-op stubs** — retained as compatibility no-ops (already documented as "per-forward billing disabled"). No forward path debits money.

### Feature Reality Matrix (PRD §80) — Batch 1 result

| Feature | DB | Service | Handler | UI | Runtime | Tests | Status |
|---|---|---|---|---|---|---|---|
| /mycode | — | Y | Y (new) | Y | Pending real-Telegram | Y (unit/integration) | VERIFIED (mock) |
| Telegram ownership (get_me + unique active owner) | Y | Y | Y | Y | Pending real-Telegram | Y (DB-level) | VERIFIED (DB-level) |
| Trial (7-day, one-time, idempotent) | Y | Y | Y (/start) | Y | — | Y | VERIFIED |
| Connect state machine + expiry cleanup | Y | Y | Y | Y | Pending real-Telegram | Y | VERIFIED (mock) |
| Forwarder pipeline no-crash + quota atomicity | — | Y | — | — | Y (repro) | Y | VERIFIED (runtime repro) |
| Telegram → Telegram | Y | Y | Y | Y | Y (pre-existing) | Y | PARTIAL (needs end-to-end) |
| Telegram → WhatsApp | Y | Y (queue) | PARTIAL | PARTIAL | BLOCKED (no real Meta creds; placeholder CDN URLs in publish job) | — | PARTIAL |
| Stars | Y | Y | — | Y | BLOCKED (no checkout/precheckout handlers, PTB invoice flow absent) | — | PARTIAL |
| UPI | Y | Y | Y | Y | BLOCKED (needs real admin verification) | Y | PARTIAL |
| Crypto | Y | Y | Y | Y | BLOCKED (needs Oxapay key) | Y | PARTIAL |
| Extra forward credits | — | — | — | — | — | — | TODO |
| Rewards/referrals | Y | Y | Y | Y | — | Y | PARTIAL |
| Support | Y | Y | Y | Y | — | Y | PARTIAL |
| AI | Y | Y | Y | Y | BLOCKED (needs key) | Y | PARTIAL |
| Filters/formatting | Y | Y | Y | Y | — | — | PARTIAL |
| Watermark | Y | Y | Y | Y | — | — | PARTIAL |
| Affiliate | Y | Y | Y | Y | — | — | PARTIAL |
| Analytics | Y | stats | PARTIAL | PARTIAL | — | — | PARTIAL |
| Navigation UX (UX-NAV-01/02) | — | — | — | legacy | — | — | TODO → Batch 2 |
| Message lifecycle/edit-in-place | — | — | — | — | — | — | TODO → Batch 2 |

## Requirement Matrix (full, one row per tracked requirement)

| ID | Requirement | Status | Files | Test | Notes |
|---|---|---|---|---|---|
| PRD §5.1–5.9 | /connect → OTP → 2FA login flow | VERIFIED* | core/user_sessions.py, bot/handlers.py, bot/states.py, main.py | test_mycode.py, test_db_ownership.py, test_import.py | *mock-level; real-Telegram run still pending (no API creds). `_time` crash fixed. |
| PRD §5.2 | /mycode (and /mycode 94563) mandatory | COMPLETE | main.py, bot/handlers.py, core/user_sessions.py | unit: extract_otp variants | legacy /myflow accepted, not primary |
| PRD §5.3 | bare numbers never OTP | COMPLETE | bot/handlers.py | integration (simulated) | |
| PRD §6.1–6.7 | external-account ownership + atomic claim + DB uniqueness | VERIFIED* | database/db.py, core/user_sessions.py | test_db_ownership.py | partial unique index; conflict = safe message; *real get_me pending |
| PRD §7.1–7.3 | encrypted sessions; no secret logs; restart recovery | PARTIAL | core/session_crypto.py, core/client_pool.py | test_import | needs_reconnect flag added; end-to-end recovery pending |
| PRD §8 | Final navigation (Projects/Subscription/Rewards/Account) | TODO | — | — | UX-NAV-01 in Batch 2 |
| PRD §9–11 | Projects + creation + T→T forwarding | PARTIAL | core/forwarder.py, services/* | runtime repro | dispatch crash fixed; full E2E pending |
| PRD §12 | Telegram → WhatsApp production | PARTIAL | core/listener.py, destinations/ | — | Cloud API queue exists; media uses placeholder CDN URLs; needs provider + media upload |
| PRD §13 | Other platforms truthful | PARTIAL | platform_registry.py | — | threads/whatsapp routes exist; instagram hidden from platform picker |
| PRD §14 | deterministic pipeline | COMPLETE | core/forwarder.py | repro | fixed dead code; single quota reservation |
| PRD §15 | content filters | PARTIAL | services/content_rules_service.py, core/forwarder.py | — | present; runtime E2E pending |
| PRD §16 | AI rewriter, fallback, limits | PARTIAL | services/ai_service.py | — | needs key for live test |
| PRD §17 | formatting | PARTIAL | services/formatting_service.py | — | |
| PRD §18 | watermark | PARTIAL | services/watermark_service.py | — | |
| PRD §19 | affiliate replacement | PARTIAL | services/affiliate_service.py | — | pattern-based; no live API |
| PRD §20 | dedup | PARTIAL | services/dedup_service.py | — | claims + confirm/release |
| PRD §21 | retry engine + dead letter | PARTIAL | services/job_queue.py, core/listener.py | — | queue worker exists |
| PRD §22 | atomic daily quota, no per-forward billing | COMPLETE | services/plan_service.py, core/forwarder.py, services/wallet_service.py | test_final_fix.py | fixed double-reserve; wallet stubs no-op |
| PRD §23 | extra forward credits | TODO | — | — | no table/service — Batch target |
| PRD §2.3/§24 | plans configurable in DB | PARTIAL | database/db.py, services/plan_service.py | pytest | stars_monthly_price column added (CREATE + late migration + seeds) — was IndexError crash; Stars checkout flow still missing |
| PRD §2.4 | one-time 7-day trial | COMPLETE | services/plan_service.py, database/db.py | test_trial.py | marker + idempotent |
| PRD §25–28 | payments: states/idempotency, Stars/UPI/Crypto | PARTIAL | services/payment_service.py, services/crypto_provider.py | — | Stars UI dead (BLOCKED); UPI/crypto manual flows exist |
| PRD §29 | referral/rewards | PARTIAL | services/referral_service.py | — | milestone+anti-abuse present |
| PRD §30–32 | Account/Connected Accounts/Wallet | PARTIAL | bot/handlers.py, bot/keyboards.py | — | |
| PRD §33–34 | Support (AI, group, owner gating, tickets) | PARTIAL | services/support_ai_service.py, support_service.py | — | |
| PRD §35 | analytics | PARTIAL | services/stats_service.py | — | per-project counters exist; UI partial |
| PRD §36 | notifications | PARTIAL | bot/notifier.py, services/notification_service.py | — | |
| PRD §37–38 | admin RBAC + pricing | PARTIAL | bot/admin_panel.py, services/audit_service.py, pricing_service.py | — | |
| PRD §39 | broadcast vs /post separation | COMPLETE | bot/admin_promo_handlers.py | — | two handlers, separate states |
| PRD §40–41 | coupons/giveaways | PARTIAL | services/coupon_service.py, giveaway_service.py | — | |
| PRD §42 | ownership checks on every object | PARTIAL | bot/handlers.py | — | helpers exist; audit continues |
| PRD §43–44 | user-keyed state + stale cleanup | COMPLETE | bot/states.py, core/user_sessions.py | unit | timestamped connect flags; single cleanup task |
| PRD §45 | rate limiting | PARTIAL | core/telegram_utils? | — | /connect flood handled via Telethon; general limiter TODO |
| PRD §46 | DB fresh/migrated/partial + indexes | VERIFIED* | database/db.py | test_db_migration.py | fresh + migration pass |
| PRD §47 | money ledger integrity | PARTIAL | services/wallet_service.py | — | dual-currency; float ledger (minor-unit refactor TODO) |
| PRD §48 | idempotency (payments, rewards, grants) | PARTIAL | services/* | — | references + unique indexes |
| PRD §49 | concurrency | PARTIAL | core/forwarder.py etc. | repro | lock/transaction audit ongoing |
| PRD §50–52 | crash recovery, health, project health | PARTIAL | core/client_pool.py, services/source_health.py | — | |
| PRD §53–54 | test actions + permission checks | PARTIAL | core/telegram_utils.py | — | |
| PRD §55–59 | media/album/ordering/links/failure policy | PARTIAL | core/forwarder.py | — | albums buffered; media cleanup present |
| PRD §60 | protected content respected | COMPLETE | core/forwarder.py | — | nofowards flag honored |
| PRD §61 | localization (7 languages) | PARTIAL | services/i18n_service.py | test_final_fix.py | EN/HI/BN/UR/ES/AR/ID keys exist; screens mostly EN hard-coded (UX-NAV-01 targets) |
| PRD §62–64 | onboarding/guide/tour | PARTIAL | bot/handlers.py | — | |
| PRD §65 | UX naming consistency | PARTIAL | — | — | UX-NAV-01 |
| PRD §66 | settings persist + used | PARTIAL | services/settings_service.py | — | |
| PRD §67–71 | audit log, suspension, maintenance, logging, rotation | PARTIAL | services/audit_service.py | — | rotation TODO |
| PRD §72 | graceful shutdown | PARTIAL | core/listener.py, main.py | — | stop_listener exists; dangling-task audit TODO |
| PRD §73–74 | requirements completeness, Pydroid | PARTIAL | requirements.txt | — | aiohttp listed; Pydroid smoke untested |
| PRD §75–77 | callback/command integrity, no dead UI | PARTIAL | bot/keyboards.py vs handlers | audit script | matrix shows likely orphans (nav:/pay:/pacct:/wacode:/aisettings:/cloneproj:/editproj:/deleteconfirm:/projcard:/aff*/wm*/ai*/lang:/analytics:) |
| PRD §78 | legacy consolidation | PARTIAL | ChannelFlowAI5_monetization/ | — | stale duplicate tree in source |
| PRD §79 | docs match reality | PARTIAL | README.md (repo) | — | full rewrite TODO (Batch final) |
| UX-NAV-01 (§92) | Unified Onboarding & Navigation UX | TODO | — | — | Batch 2 Feature 1 (PRD updated, tracker row added) |
| UX-NAV-02 (§93) | Task-first navigation & message lifecycle | TODO | — | — | Batch 2 Feature 2 (PRD updated, tracker row added) |
| UX-NAV-03..06 (§94) | Discovered UX backlog | TODO | — | — | doc-only backlog, prioritize later |

\* VERIFIED = verified at the level possible without live Telegram credentials (unit/integration/DB/runtime repro). Live-account E2E remains the documented limitation.

## Batch History

### Batch 1 — Core Identity & Forwarding Integrity (2026-09-07)

Features implemented (5):
1. `/mycode` OTP command + login-code input routing (PRD §5.2/5.3/5.6/5.9) — fixes `_time` crash (§5.8), text-route OTP parsing, stage-password flow.
2. Real external-account identity + atomic ownership (PRD §6): `get_me()` capture, `external_telegram_id`, partial unique index `idx_uts_one_active_owner`, transactional claim with cross-user-safe conflict error, disconnect releases ownership.
3. Restart-recovery + stale-login-state lifecycle (PRD §7.3/§43/§44/§45): `needs_reconnect` flagging in client_pool, timestamped connect flags, single cleanup task started by listener.
4. One-time 7-day trial at /start (PRD §2.4/§83/§82.1): `trial_granted_at` marker, idempotent persistent `start_trial`, welcome message states trial active.
5. Forwarding engine integrity (PRD §14/§22/§22.1): fixed unreachable-`owner_id` dead code crash (`UnboundLocalError`), single atomic quota reservation per message, no double-reserve, failed forwards release quota.

Bugs fixed in Batch 1: `_time` NameError (login timeout); `/mycode` missing; `user_id` NameError in `/start` connected path; `WAITING_CONNECT_STAGE` cleanup import/type bug; trial re-grant loophole; `_dispatch` dead-code crash + double quota reservation; stale per-forward-billing test assertion.

Files changed:
- core/user_sessions.py (rewritten login/ownership/cleanup core)
- core/client_pool.py (needs_reconnect marking)
- core/forwarder.py (dispatch reservation fix)
- core/listener.py (cleanup-task wiring — via start_cleanup_task single)
- bot/handlers.py (connect/mycode/start/trial/welcome/cancel)
- bot/states.py (WAITING_CONNECT_STAGE_STARTED)
- bot/keyboards.py (backing helpers unchanged)
- services/plan_service.py (start_trial marker)
- services/wallet_service.py (unchanged stubs; test updated instead)
- database/db.py (users.trial_granted_at, sessions ownership columns + partial unique index,
  plan_configs.stars_monthly_price column + seeds + late migration, guarded index creation order)
- main.py (/mycode + unknown-command router)
- tests/test_mycode.py, tests/test_db_ownership.py, tests/test_forwarder_integrity.py,
  tests/test_no_per_forward_billing.py (new; pytest suite)
- test_final_fix.py (per-forward assertion removed → no-billing)
- PRD.md (§92–94 appended), tracker.md (this file)

Tests executed (Batch 1):
- syntax compileall: PASS
- import checks (fresh env, dummy .env): PASS
- database fresh init: PASS
- database existing-DB migration (init on pre-existing v1 schema): PASS (test_db_migration)
- unit: OTP extraction formats, trial idempotency/expiry, ownership DB conflict/claim, connect-conflict message safety: PASS
- runtime repro forwarder dispatch (before fix: UnboundLocalError; after: no crash): PASS
- no-per-forward-billing regression (updated test_final_fix.py): PASS
- callback/command audit: RUN (full fix is Batch 2/3 scope)

Test result: PASS (see notes)

Remaining issues: live-Telegram E2E not runnable without real API credentials (documented, BLOCKED); nav/pay callback orphans & UI restructure deferred to UX-NAV-01/02; Stars checkout flow missing (deferred); extra-credit system missing (deferred).

Commit/version identifier: none yet (working tree on arena branch; checkpoints below)

ZIP generated: `ChannelFlowAI_Batch01_CHECKPOINT.zip` (repo root; excludes secrets/DBs/caches)

Next batch: Batch 2 = UX-NAV-01 (Unified Onboarding & Navigation UX) + UX-NAV-02 (Task-First Navigation & Message Lifecycle) + 3 more high-priority fixes (candidate: callback integrity cleanup for nav:/pay:/pacct:/wacode:/cloneproj; Stars checkout/precheckout; extra-forward-credit system).

## Resume Point

```
CURRENT BATCH:              Batch 1 — COMPLETE (checkpoint ZIP created; awaiting user go-ahead)
LAST COMPLETED FEATURE:     #5 Forwarding-engine quota/integrity fix (Batch 1)
LAST VERIFIED FEATURE:      #5 (runtime repro PASS); #1–4 verified at unit/integration/DB level
NEXT FEATURE:               Batch 2 Feature 1 = UX-NAV-01 Unified Onboarding & Navigation UX
KNOWN BUGS:                 [open] (b) orphan callbacks nav:/pay:/pacct:/wacode:/ai*/aff*/wm*/lang:/
                            analytics:/editproj:/projcard:/cloneproj:/deleteconfirm: have no handlers in
                            button_handler → UX-NAV batch; (c) media publish for WhatsApp uses placeholder
                            CDN URLs (provider work); (d) whatsapp_pairing seed block inserts sample codes
                            for first user at DB init (cleanup candidate); (e) ChannelFlowAI5_monetization/
                            duplicate handler tree (legacy consolidation); (f) core/forwarder.py imports
                            core.client at module import → TelegramClient(SESSION_NAME,...) construction
                            side-effect creates an empty ChannelFlow.session file on any import (gitignored;
                            lazy-construction cleanup candidate)
                            [fixed in Batch 1] (a) stars_monthly_price column gap in plan_configs crashed
                            get_entitlements on fresh/existing DBs → column added to CREATE + late migration
                            + seeds (FREE 0 / BEGINNER 99 / PRO 249 / CREATOR 499)
KNOWN BLOCKERS:             live Telegram account test (no real API_ID/API_HASH/BOT_TOKEN in sandbox);
                            Meta WhatsApp Cloud API credentials; Oxapay key; OpenRouter key
NEXT TESTS:                 Batch 2 test list: language select/persist, /start idempotency, unconnected
                            + connected menus, Back/Home context, disconnect → unconnected menu,
                            callback authorization, two-user isolation, EN + Hinglish, message-edit
                            fallback, login cleanup, plus full callback/command integrity audit
```

## Deployment & Test Notes

- Run from `ChannelFlow_Bot/`: copy `.env.example` → `.env`, fill BOT_TOKEN/API_ID/API_HASH/SESSION_ENCRYPTION_KEY (+ optional UPI/OXAPAY/OPENROUTER/WhatsApp keys), `pip install -r requirements.txt`, `python main.py`.
- Tests: `tests/` (new, pytest-compatible) + legacy `test_*.py` in project root.
- The repository-root `ChannelFlow_Bot.zip` is the immutable base; never overwrite it. Final delivery replaces it with the checkpoint chain per user rules.
