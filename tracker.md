# ChannelFlow AI Implementation Tracker

## Project

- Repository: https://github.com/TheOgcoderGit/musical-giggle
  - Local branch: `arena/01a07adf-musical-giggle` (work tree rooted at repo root; project source lives in `ChannelFlow_Bot/`)
  - Upstream `main` contains only: `ChannelFlow_AI_Master_PRD.md`, `ChannelFlow_Bot.zip` (base, 299,994 bytes), `README.md`
- Base ZIP: `ChannelFlow_Bot.zip` in repo root (the 2026-09-05 upload; the only base provided — no FIXED_v4 or older duplicate was used)
- PRD: `ChannelFlow_AI_Master_PRD.md` (3452 lines, now extended to §94 with UX-NAV-01/02/03–06)
- Current implementation batch: **Batch 3** (in progress → completed by this checkpoint)
- Last completed batch: Batch 1/2 (see Batch History) and **Batch 3 = Telegram Stars checkout backend** (this checkpoint)
- Last tested batch: Batch 3 (tests executed below; results in Batch History)
- Next starting point: next batch after product owner go-ahead (candidates in Resume Point) — see Resume Point

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
| Stars (plan checkout, §26) | Y | Y | Y (upgrade:splans chain + pre_checkout/successful_payment) | Y (⭐ row on upgrade picker; no dead buttons) | Pending real-Telegram (invoice sheet is client-side) | Y (23 new) | VERIFIED (mock/simulation) |
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
| UX-NAV-01 (§92) | Unified Onboarding & Navigation UX | Y (users.language_chosen) | Y (i18n_service keys/flag) | Y (handlers/menu) | Y (reply menus + hub screens) | Pending live-Telegram | Y (unit/sim: 24 new) | VERIFIED (mock/simulation) |
| UX-NAV-02 (§93) | Task-first navigation & message lifecycle | — | Y (nav_state module) | Y (nav:/projcard:/editproj:/deleteconfirm:/… routes) | Y (task list/detail/edit/confirm) | Pending live-Telegram | Y (unit/sim) | VERIFIED (mock/simulation) |
| UX-NAV-03..06 (§94) | Discovered UX backlog | TODO | — | — | doc-only backlog, prioritize later (explicitly NOT implemented out of order) |

\* VERIFIED = verified at the level possible without live Telegram credentials (unit/integration/DB/runtime repro). Live-account E2E remains the documented limitation.

## Batch History

### Batch 3 — Telegram Stars checkout backend (PRD §26) (2026-09-07)

PRD §26 was the top documented remaining gap after Batch 2 (UI fully
gated, no dead buttons; backend absent). This batch implements the real
Stars purchase loop end to end at the service + handler level.

Features implemented:
1. **`services/stars_service.py` (NEW - pure service layer, no telegram
   import, offline-unit-testable)**: DB-driven Stars pricing
   (`plan_configs.stars_monthly_price` x `plan_durations.discount_percent`,
   integer-only totals, discount floor-rounded DOWN to a whole Star so the
   user never pays more than the exact discounted amount);
   `create_plan_invoice_row` snapshot rows (PENDING_PAYMENT / method=stars /
   currency=STARS / purpose=plan / 15-min expiry, same policy as crypto);
   `resolve_stars_success` - the money-to-plan boundary validating in
   order: row exists -> method=stars -> ownership -> still pending ->
   not expired (flips EXPIRED) -> Telegram-paid Stars == snapshot
   final_amount; then an atomic guarded UPDATE flips the row to SUCCESS
   exactly once (duplicate replay/restart redelivery is a no-op) and
   activates the plan (shared idempotent activation + last_purchase
   markers + referral check). `cancel_request` (send_invoice failures),
   `get_stars_options`, `get_user_stars_history`.
2. **Inline UI chain (bot/handlers.py + bot/keyboards.py)**:
   `acct:upgrade` now has a "⭐ Telegram Stars (instant)" row (DB prices,
   current-plan marker) -> `upgrade:splans` -> `upgrade:splan:{plan}` ->
   `upgrade:sduration:{plan}:{months}` -> `upgrade:scheckout:{plan}:{months}`
   opens the Telegram payment sheet via `send_invoice(currency="XTR",
   payload="xtr:plan:<row-id>", provider_token="")` with the snapshot
   amount; failure cancels the row and tells the user. Stars screens edit
   in place (nav-state message lifecycle). Old "Stars not available yet"
   copy replaced. The 3 new keyboard builders were added to the displayed-
   keyboard audit (they emit only the handled `upgrade` prefix).
3. **`pre_checkout_query` handler**: answers ok=False (Telegram shows the
   error, user is never charged) for unknown payloads, foreign/missing
   request rows, non-pending rows, wrong currency, or total_amount != the
   DB snapshot; ok=True only when everything verifies.
4. **`successful_payment` handler**: re-validates via resolve_stars_success,
   then sends the receipt ("welcome to <plan>") with the account-state
   menu; expired invoices get the refund copy (Telegram auto-refunds);
   replays/mismatches refuse without ever granting anything.
5. **main.py**: PreCheckoutQueryHandler + MessageHandler
   (filters.SUCCESSFUL_PAYMENT) registered before the error handler.
6. **Latent runtime bug found + fixed by the new chain test**:
   `acct:upgrade` referenced the nonexistent `payment_service.PLAN_PRICES`
   (AttributeError on every tap -> stale-callback fallback). Now priced
   from `plan_service.get_plan_crypto_price_usd` (DB USD price book).

Files changed:
- services/stars_service.py (NEW)
- bot/keyboards.py (stars_plan/duration/confirm keyboard builders)
- bot/handlers.py (upgrade:splans/splan/sduration/scheckout chain, Stars
  row + copy, pre_checkout_handler, successful_payment_handler, PLAN_PRICES
  latent-bug fix)
- main.py (two new handler registrations)
- tests/test_stars_batch3.py (NEW - 23 tests)
- tests/test_uxnav_batch2.py (audit list extended with the 3 Stars builders)
- tracker.md (this file)

Tests executed (Batch 3):
- pytest suite: 71 passed (48 Batch-1/2 + 23 Batch-3 new)
- standalone regressions: test_final_fix.py ALL REGRESSION TESTS PASSED;
  test_db_migration.py / test_db.py PASS
- compileall across bot/services/database/core/destinations/tests/main.py: PASS
- import gate (fresh env): bot.handlers / bot.keyboards / stars_service /
  main.py wiring parse: PASS

Test result: PASS (live-Telegram E2E still BLOCKED - no real credentials;
the Stars payment sheet itself is a Telegram-client step that cannot run
in the sandbox, but pre-checkout/successful_payment logic is fully
covered offline through the real handlers).

Remaining issues after Batch 3: live Telegram E2E BLOCKED; extra-credit
package purchases via Stars need the extra-credit backend first (deferred
feature, not UI); wallet top-up intentionally still UPI/crypto only
(wallet bookkeeping is INR/USD; a Stars-denominated top-up needs an
explicit rate decision); inner-screen EN/HI i18n rollout ongoing; legacy
unreferenced keyboard sections (bug e) + ChannelFlowAI5_monetization tree
still to consolidate; WhatsApp CDN placeholder URLs (bug c);
whatsapp_pairing seed sample codes (bug d); core.client import side-effect
(bug f).

Commit/version identifier: see git log (Batch 3 commit after this tracker update).

ZIP generated: `ChannelFlowAI_Batch03_CHECKPOINT.zip` (repo root; excludes secrets/DBs/caches)

Next batch: candidates = UX-NAV-03..06 backlog (§94) when scheduled by
the product owner, extra-credit packages (backend + Stars purchase),
Stars wallet top-up (after a rate policy decision), full-HI copy pass.

### Batch 2 — UX-NAV-01 Unified Onboarding & Navigation UX + UX-NAV-02 Task-First Navigation & Message Lifecycle (2026-09-07)

Tracked as ONE feature batch per the product-owner UX requirement (PRD §92/§93, appended verbatim in Batch 1; §94 backlog documented but NOT implemented out of order).

Features implemented:
1. **Language-first /start (92.1/92.7)**: `users.language_chosen` column (default 0; guarded ALTER for existing DBs); first `/start` shows the en/hi picker (`FIRST_RUN_LANGUAGE_KEYBOARD`, callback `lang:*`), selection persists via `i18n.set_user_language` (+flag); repeated `/start` never re-asks, never duplicates users/trials/sessions; picker is bypassed for users who already chose.
2. **Account-state persistent menus (92.2/92.3)**: unconnected menu (Connect Account / Why Connect / Subscription / How It Works / Support) vs connected menu (Projects / Subscription / Rewards / Account / Support / Settings); every label is matched by `menu_handler` (labels shared from keyboards.py constants - no more dead "🏠 Home"/"⚙️ Settings" reply rows that fell through to the fallback). All legacy reply sites route through state-aware `_reply_menu_for`; disconnect re-attaches the unconnected menu and clears connected screens (92.3 auto-return).
3. **Back/Home everywhere + centralized navigation state (92.4–92.6 / 93.4)**: new `bot/nav_state.py` - per-user screen stack (never global), `place()` message-lifecycle choke point, pure helpers (`home_variant`, `expired_for`, ...) unit-tested. `nav:*` (home/projects/account/settings/help), `lang:*`, `newproj`, `projcard`, `editproj`, `deleteconfirm`, `deletesourceconfirm`, `deletedestinationconfirm`, `filterkw/filterdomains/filtersenders`, `clearfiltersconfirm`, `fmtroot`, `support:*`, `help:*` handlers implemented; stale callbacks answer "screen expired → Home".
4. **Task-first hub (93.1/93.2)**: 📁 Projects = task LIST first (one row per task) with empty state ("Create Your First Task"); Task Details card compact (status/route/sources/destinations + Start-Pause + Sources/Destinations/Filters/Test/Stats/Edit/Delete + Home/Tasks); delete always confirms then refreshes the list (empty state on last delete); start/stop/stats/logs edit the dashboard message in place instead of stacking duplicates.
5. **Message lifecycle (93.3)**: interactive hub screens edit in place when triggered from an inline button; fresh renders delete the previously tracked BOT message (never user messages - deletion only ever targets bot-sent screens recorded in the per-user stack, and only via `Chat.delete_message` on ids we stored); on edit errors fall back delete-and-resend; OTP/2FA category-C cleanup untouched (never logs values).
6. **Support hub + tickets usable pre-login (92.2 companion)**: Support AI chat (`support:ai`), Support Team/FAQ/Guide/Tour, My Tickets list/view/reply/close/reopen with ownership checks, New Ticket (category → subject → body), Feature Request (`help:feedback`) stored as a `feature` ticket for ANY user incl. never-connected anonymous users (companion-defect fix - no more crash/strand on unconnected feature requests).
7. **Companion defects carried in this batch**:
   - Orphan-callback UI-integrity: every callback statically produced by DISPLAYED keyboards now has a handler (audit test `test_no_dead_buttons_on_displayed_keyboards`); dead rows removed/rewritten (Settings hub, Account hub, project dashboards, filter/formatting sub-screens, Instagram "Caption Formatting" row removed); admin:payments sub-screen implemented (was "⚠ Unknown Admin Action").
   - Stars gate: no displayed keyboard renders Stars/pay:* buttons (payment UX uses the live upgrade:* chain; plan/billing screens explain Stars is "not available yet" as text). Real invoice/precheckout flow remains a documented future batch.
   - `main_menu`/`pre_login_menu` reply keyboards realigned to the account-state menus (base had label mismatches: "🏠 Home" and "⚙️ Settings" reply rows were NOT handled by menu_handler and dead-ended in the fallback).

Bugs fixed in Batch 2: dead "🏠 Home"/"⚙️ Settings" reply rows; orphan callbacks on every displayed project/settings/account/support keyboard (nav:, projcard:, newproj, editproj:, deleteconfirm:, filterkw:, fmtroot:, deletesourceconfirm:, etc.); settings hub with 7/9 dead rows; account hub dead rows; admin:payments dead row; anonymous feature-request gap; sqlite Row `.get()` crashes in new task-detail paths; `project_actions_keyboard` showed Start AND Pause when running state was not passed (one was always dead → resolves status from DB).

Files changed:
- database/db.py (users.language_chosen + guarded migration)
- bot/keyboards.py (MB_* label constants; account-state reply menus; rewritten displayed keyboards: project actions/task detail, edit root, account hub, settings hub; Stars row gated in reference payment keyboards; banner marking UNREFERENCED legacy sections)
- bot/states.py (WAITING_SUPPORT_AI / WAITING_TICKET_SUBJECT/MESSAGE/REPLY)
- services/i18n_service.py (UX-NAV keys EN+HI: lang.*, nav.home_connected/home_unconnected, nav.why_connect, nav.your_tasks/tasks_empty, task.details/deleted/delete_confirm/created/started/stopped, nav.screen_expired, nav.back/home, support.why_connect_hint; `set_user_language` sets language_chosen=1)
- bot/nav_state.py (NEW - per-user nav stack + place() lifecycle + pure helpers)
- bot/handlers.py (/start language-first + home routing; menu_handler account-state routing + support/ticket/feedback waits; ~20 new callback actions incl. nav/lang/newproj/projcard/editproj/deleteconfirm/deletesourceconfirm/deletedestinationconfirm/filter*/clearfiltersconfirm/fmtroot/support:*/help:*/acct:plan|wallet|connections/admin:payments; settings:language/systatus/disconnect re-state; start/stop/stats/logs/delete edit-in-place; `_reply_menu_for` sweep)
- tests/test_uxnav_batch2.py (NEW - 24 tests)

Tests executed (Batch 2):
- pytest suite: 48 passed (24 Batch-1 + 24 Batch-2 new)
- legacy standalone regressions: test_db_migration.py / test_final_fix.py / test_db.py ALL PASS
- compileall across bot/services/database/core/destinations/tests/main.py: PASS
- simulation smoke (fake updates/messages): unconnected + connected menu routing, task list→detail→edit→list chain, plan/account/settings/support hubs, nav:home inline edit, settings:disconnect → unconnected state, post-disconnect gate: PASS
- UI-integrity callback audit (test): PASS - no orphan prefixes on displayed keyboards, no pay:/Stars rows

Test result: PASS (live-Telegram E2E still BLOCKED - no real credentials in sandbox)

Remaining issues after Batch 2: live Telegram E2E BLOCKED (sandbox has no real API creds); Stars checkout/precheckout (invoice flow) still not implemented - UI fully gated, backend work deferred; several inner screens (plan/rewards/settings hub bodies, deep filter/formatting prompts) still English hard-coded - EN/HI i18n rollout for all copy is a future pass (existing legacy languages preserved for existing keys); unreferenced reference-bot keyboard sections in keyboards.py still carry orphan payloads (never rendered - cleanup candidate, bug (e) family); WhatsApp CDN placeholder URLs (bug c), whatsapp_pairing seed sample codes (bug d), core.client import side-effect ChannelFlow.session (bug f).

Commit/version identifier: see git log (Batch 2 commit after this tracker update).

ZIP generated: `ChannelFlowAI_Batch02_CHECKPOINT.zip` (repo root; excludes secrets/DBs/caches)

Next batch: candidates = UX-NAV-03..06 backlog (pagination, plan-locked CTA states, loading/error-state consistency, onboarding chips) when the product owner schedules them, plus remaining open bugs (c/d/e/f), Stars real checkout, full-HI copy pass.

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
CURRENT BATCH:              Batch 3 — COMPLETE (Telegram Stars checkout backend; checkpoint ZIP + commit
                            pushed; awaiting user go-ahead for the next batch)
LAST COMPLETED FEATURE:     Telegram Stars plan checkout (PRD §26) - send_invoice XTR chain +
                            pre_checkout + successful_payment + idempotent snapshot activation
LAST VERIFIED FEATURE:      Stars checkout verified at service + handler + simulation level
                            (71 pytest tests PASS incl. 23 new; legacy regressions PASS)
NEXT FEATURE:               TBD by product owner — documented candidates: UX-NAV-03..06 backlog (§94),
                            extra-credit packages (backend + Stars purchase), Stars wallet top-up
                            (needs a Stars→INR/USD rate policy decision), WhatsApp CDN placeholder
                            URLs, whatsapp_pairing seed cleanup, ChannelFlowAI5 duplicate tree
                            consolidation, lazy core.client construction, full EN+HI copy pass
KNOWN BUGS:                 [open] (c) media publish for WhatsApp uses placeholder CDN URLs (provider
                            work); (d) whatsapp_pairing seed block inserts sample codes for first user at
                            DB init (cleanup candidate); (e) keyboards.py unreferenced reference-bot
                            keyboard sections (pay:/pacct:/wacode:/ai/aff/wm payloads, never rendered —
                            banner-marked; consolidation candidate) + ChannelFlowAI5_monetization/
                            duplicate handler tree; (f) core.client import side-effect creates empty
                            ChannelFlow.session file on any import (gitignored; lazy-construction
                            cleanup candidate)
                            [fixed in Batch 3] (g) Stars purchase backend: send_invoice XTR +
                            pre_checkout_query + successful_payment implemented; upgrade:PLAN_PRICES
                            latent AttributeError fixed (nonexistent constant, every acct:upgrade tap
                            crashed → stale-callback reply); Stars row live on the upgrade picker
                            [fixed in Batch 2] (b) orphan callbacks on DISPLAYED keyboards (see Batch 2
                            history)
                            [fixed in Batch 1] (a) stars_monthly_price column gap in plan_configs
KNOWN BLOCKERS:             live Telegram account test (no real API_ID/API_HASH/BOT_TOKEN in sandbox);
                            the Stars payment sheet is a Telegram-client step (can only be exercised
                            with a real bot); Meta WhatsApp Cloud API credentials; Oxapay key;
                            OpenRouter key
NEXT TESTS:                 next-batch test list (candidates above), plus rerun of the 71-test suite +
                            UI-integrity audit after every UI change
```

## Deployment & Test Notes

- Run from `ChannelFlow_Bot/`: copy `.env.example` → `.env`, fill BOT_TOKEN/API_ID/API_HASH/SESSION_ENCRYPTION_KEY (+ optional UPI/OXAPAY/OPENROUTER/WhatsApp keys), `pip install -r requirements.txt`, `python main.py`.
- Tests: `tests/` (new, pytest-compatible) + legacy `test_*.py` in project root.
- The repository-root `ChannelFlow_Bot.zip` is the immutable base; never overwrite it. Final delivery replaces it with the checkpoint chain per user rules.
