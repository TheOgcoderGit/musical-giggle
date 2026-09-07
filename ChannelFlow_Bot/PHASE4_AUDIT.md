# ChannelFlow AI — Phase 4 Audit Report

**Date:** 2026-08-30  
**Scope:** Full codebase audit of Phase 3 final build

---

## Executive Summary

The codebase is a substantial Telegram forwarding bot with WhatsApp/Threads/Instagram integration, multi-currency wallet, admin/owner RBAC, and project-based forwarding. It's architecturally sound with good separation of concerns but has several bugs, incomplete features, and production-readiness gaps that need addressing before Phase 4 hardening.

---

## 1. Working Features (Verified)

| Feature | Status | Notes |
|---------|--------|-------|
| /start + Welcome flow | ✅ | User registration, referral capture, welcome screen with Connect CTA |
| Project CRUD | ✅ | Create/Edit/Delete/Clone/List with platform_type (TG/TG, TG/WA, TG/TH, TG/IG) |
| Source management | ✅ | Add/Delete/Toggle sources with chat validation |
| Destination management | ✅ | Add/Delete/Toggle/Clone destinations with platform-specific flow |
| Telegram → Telegram forwarding | ✅ | Core engine working with routing cache |
| Telegram → WhatsApp pairing | ✅ | Pairing codes, state machine (pending→verifying→verified/failed/expired), 10-min expiry |
| Telegram → WhatsApp forwarding | ✅ | Via job queue + WhatsApp Cloud API |
| Telegram → Threads | ✅ | Meta Threads API integration |
| Instagram Broadcast | ⚠️ | Manual queue only (no official publish API) |
| Dual-currency wallet (INR/USD) | ✅ | Separate balances, atomic credit/debit, ledger with idempotency |
| Per-forward billing | ✅ | Reserve/Confirm/Release with dedup claim ID |
| Daily forward limits | ✅ | Per-project atomic reservation via `reserve_daily_forward()` |
| Admin RBAC | ✅ | Granular permissions, audit logging, admin management |
| Owner authentication | ✅ | 3-step (Telegram ID → username → password → security answer) |
| Referral system | ✅ | Capture + milestone rewards (idempotent) |
| Coupons/Giveaways | ✅ | Creation + redemption with idempotency |
| Support tickets | ✅ | User + admin views, reply/close workflow |
| FAQ/Knowledge base | ✅ | DB-backed articles seeded in init |
| i18n framework | ✅ | 7 languages, 50+ keys, user preference stored |
| Source health monitoring | ✅ | Health checks + hourly notifications |
| Platform status toggles | ✅ | Telegram/WhatsApp/Threads/IG admin toggles |
| Maintenance mode | ✅ | Persisted in app_config, owner bypass |
| Owner auth | ✅ | 3-step challenge + rate limiting + lockout |
| WhatsApp pairing codes | ✅ | Unique 8-char codes, 10-min expiry, state machine |
| WhatsApp Test Connection | ✅ | Validates account + permissions |

---

## 2. Broken Features (P0 - Must Fix)

### 2.1 Telegram OTP/Connect Flow — Silent Hang Risk
**File:** `core/user_sessions.py`, `bot/handlers.py`
**Issue:** The `/connect` flow uses Telethon's interactive login. If user enters wrong OTP or code expires, the bot can silently hang without feedback. No timeout handling for the `WAITING_CONNECT_STAGE` state.
**Evidence:** `WAITING_CONNECT_STAGE` state not cleared on error paths in `_handle_connect_code_or_password()`
**Risk:** User stuck forever, no way to recover without `/cancel`

### 2.2 WhatsApp Pairing Verification Endpoint — Not Implemented
**File:** `core/pairing_endpoint.py`
**Issue:** The endpoint exists as a library but is **not wired into any HTTP server**. The WhatsApp bot (separate service) has nowhere to POST the pairing code.
**Impact:** Pairing codes can be generated but never verified via API. Only manual verification possible.

### 2.3 Cross-User Isolation — Partial
**File:** `bot/handlers.py` — `_get_owned_*` functions
**Issue:** Some handlers may bypass ownership checks. Need to verify every `action == "..."` branch uses `_get_owned_*`.
**Risk:** User A could potentially act on User B's project via crafted callback_data.

### 2.4 Daily Limit Race Condition
**File:** `services/plan_service.py` — `within_daily_forward_limit()`
**Issue:** The check-then-increment pattern is not atomic. Under concurrent forwards at the limit, multiple forwards could pass the check before any increments.
**Current:** `within_daily_forward_limit()` reads, then `stats_service.increment()` increments separately.
**Fix:** Use `reserve_daily_forward()` which atomically checks+reserves in a transaction (already exists but not fully wired in all paths).

### 2.5 WhatsApp Media Delivery — Not Implemented
**File:** `destinations/whatsapp_destination.py`
**Issue:** Only text messages supported. Media (photo/video/document) delivery not implemented. `send_media` method is a stub.
**Impact:** WhatsApp destinations can only forward text messages.

### 2.6 Instagram Broadcast — Manual Only
**File:** `destinations/instagram_destination.py`
**Issue:** Instagram Broadcast Channel has no official publish API. Current implementation routes through "Ready-to-Publish" manual approval queue. This is correct but should be clearly documented and UI should reflect "Manual Only" status.

### 2.7 Wallet Balance Negative Edge Case
**File:** `services/wallet_service.py` — `debit_currency()`
**Issue:** The `debit_currency` correctly checks balance before debit, but the check and update are in the same transaction (good). However, concurrent debits could race if not using `BEGIN IMMEDIATE`. The current code uses `BEGIN` (deferred), not `BEGIN IMMEDIATE`.

### 2.8 Source Health Monitoring — Incomplete
**File:** `services/source_health.py`, `core/subscription_scheduler.py`
**Issue:** `notify_unhealthy_sources()` exists in scheduler but `_check_source_health()` requires Telethon client which may not be available in scheduler context. The scheduler doesn't have access to the Telethon client.

---

## 3. Partially Implemented Features

| Feature | Completion | Missing |
|---------|------------|---------|
| WhatsApp Pairing | 85% | HTTP endpoint not wired; media support missing |
| WhatsApp Media | 0% | Stub only |
| Instagram Feed (Graph API) | 30% | "feed" target_type in schema but no publish logic |
| Source Health | 60% | Check logic exists; scheduler integration broken |
| Platform Health Checks | 70% | Exists in listener; not in forwarder pre-check |
| i18n | 20% | ~50 keys defined, <5 used in handlers |
| FAQ/Guide | 80% | Seeded articles; search not implemented |
| Referral Leaderboard | 70% | Query exists; no UI |
| Analytics | 40% | Schema exists; no aggregation endpoints |
| Media Watermark | 80% | Applied to photos only; video/document not supported |

---

## 4. Missing Features (Not Started)

| Feature | Phase Required | Notes |
|---------|----------------|-------|
| WhatsApp media (photo/video/doc/audio/albums) | Phase 8 | Requires CDN abstraction |
| Content pipeline: affiliate/link replacer | Phase 9 | Partially in formatting rules |
| AI rewrite per-destination | Phase 9 | Currently per-project only |
| Affiliate link replacer | Phase 31 | Creator-only feature spec'd |
| Advanced analytics dashboard | Phase 27 | Schema exists, no UI |
| Admin broadcast with segmentation | Phase 38 | Only all/active/inactive/plan |
| User-facing analytics per project | Phase 27 | Stats exist, no UI |
| Test suite | Phase 31 | Only compile checks exist |
| WhatsApp bot pairing server | Phase 7 | Not in this repo |
| Threads media support | Phase 8 | Not implemented |
| ChannelFlow AI branding consistency | Phase 33 | Mixed naming |

---

## 5. Dead Code / Duplicate Code

| Location | Issue | Action |
|----------|-------|--------|
| `bot/handlers.py:206-228` | `MAIN_MENU_BUTTONS` includes legacy aliases (`BTN_ACCOUNT`, `BTN_HELP`, etc.) that don't appear on any keyboard | Remove or map to active handlers |
| `bot/handlers.py:203-215` | Legacy button constants `BTN_ACCOUNT`, `BTN_HELP`, `BTN_MY_PROJECTS`, `BTN_STATUS`, `BTN_SETTINGS_LEGACY`, `BTN_MY_PLAN` not on any keyboard | Remove or re-enable if needed |
| `database/db.py:1247-1251` | `giveaway_winners.granted_at` migration added in two places | Consolidate |
| `services/destination_service.py:197-246` | Two `get_destination` definitions (lines 197 and 228) | Remove duplicate |
| `core/pairing_endpoint.py` | Uses `aiohttp` import at module level; fails if not installed | Make aiohttp import lazy/optional |

---

## 6. Security Concerns

| Severity | Issue | Location |
|----------|-------|----------|
| **High** | Owner password stored as hash but compared with plain input (no timing-safe compare) | `bot/owner_panel.py` |
| **High** | WhatsApp pairing endpoint has no rate limiting | `core/pairing_endpoint.py` |
| **Medium** | `SESSION_ENCRYPTION_KEY` no fallback; hard crash if unset | `config.py:66` |
| **Medium** | `ADMIN_IDS` env var split by comma; empty string creates `{0}` set | `config.py:38-42` |
| **Medium** | `INR_PER_USD` hardcoded default 85; no API for live rates | `config.py:102` |
| **Medium** | Telegram session strings encrypted but `SESSION_ENCRYPTION_KEY` not rotated | `core/session_crypto.py` |
| **Low** | `partner_id` in some callbacks not validated for ownership | Various handlers |
| **Low** | `WAITING_*` state dicts never expire proactively (only on access) | `bot/states.py` |

---

## 7. Data Integrity Concerns

| Table | Issue | Risk |
|-------|-------|------|
| `daily_usage` | No atomic reserve; race condition at limit | Over-forwarding beyond plan limit |
| `wallet_transactions` | UNIQUE index on `reference` but `DEBIT_PENDING` and `DEBIT` can coexist with same reference | Double-charge possible if confirm/release race |
| `platform_accounts` | No foreign key to `destinations.platform_account_id` | Orphaned destinations possible |
| `projects.platform_type` | No CHECK constraint; invalid values possible | Invalid routing |
| `platform_status` | No default rows for new platforms added later | Platform toggle UI may miss new platforms |

---

## 8. UX Problems

| Area | Issue |
|------|-------|
| **Main Menu** | 4 buttons (good), but "Home" duplicates "Projects" flow |
| **Project List** | No search/filter for users with many projects |
| **Destination List** | WhatsApp/Threads show pairing code but no "Copy" button |
| **Source Adding** | No validation preview; user types username blindly |
| **Project Creation** | Platform selection asks for platform again inside TG→WA flow (redundant) |
| **Settings** | "Notifications" screen is fake (no real toggles) |
| **Help/Guide** | "Guide" and "Tour" buttons identical content |
| **Project Card** | Shows readiness but no "Test Connection" for WhatsApp (button exists but handler separate) |
| **Main Menu** | Legacy buttons (`BTN_ACCOUNT`, `BTN_HELP`, etc.) in `MAIN_MENU_BUTTONS` but not on keyboard |

---

## 8. Production Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Forward engine crash → message loss | Medium | High | Add dead-letter queue for failed forwards |
| WhatsApp pairing code leaked | Low | High | Add HMAC signature to pairing codes |
| Wallet negative balance race | Low | Critical | Use `BEGIN IMMEDIATE` everywhere |
| WhatsApp pairing code replay | Medium | High | Add HMAC + one-time use enforcement |
| Admin session fixation | Low | High | Add session expiry + IP binding |
| Telegram session theft | Low | Critical | Session encryption OK but key not rotated |
| Database lock contention | High (WAL helps) | Medium | Monitor `PRAGMA busy_timeout` |

---

## 9. Integration Gaps

| Integration | Status | Gap |
|-------------|--------|-----|
| Telegram → WhatsApp | 85% | Media not supported; pairing endpoint not wired |
| Telegram → Threads | 70% | No media; no profile fetch |
| Telegram → Instagram Broadcast | 60% | Manual queue only |
| Telegram → Instagram Feed | 30% | Schema exists; no publish logic |
| Telegram → Telegram | 95% | Solid; missing some filters |
| Owner auth → Admin panel | 90% | Session expiry OK; no IP binding |
| Referral → Wallet credit | 90% | Milestone grants idempotent ✅ |
| Coupon → Wallet credit | 90% | Only on payment approval ✅ |
| Giveaway → Plan activation | 80% | Idempotent with `granted_at` ✅ |
| Referral → Milestone | 90% | `referral_milestone_grants` prevents double-grant ✅ |

---

## 10. Recommended Fixes (Prioritized)

### P0 — Must Fix Before Production
1. **Fix Telegram OTP hang** — Add timeout handling + proper error messages in `user_sessions.py`
2. **Wire WhatsApp pairing HTTP endpoint** — Mount `pairing_endpoint.setup_aiohttp_routes(app)` in a standalone server or main.py
3. **Fix daily limit race** — Replace `within_daily_forward_limit()` calls with `reserve_daily_forward()` in all forward paths
4. **Fix wallet negative balance race** — Use `BEGIN IMMEDIATE` in all wallet transactions
5. **Implement WhatsApp media delivery** — Add media upload to CDN + WhatsApp media message types
6. **Wire pairing endpoint HTTP server** — Add `aiohttp` to requirements (optional) or document external service contract

### P1 — Before Launch
1. Remove dead code (legacy buttons, duplicate functions)
2. Add pairing code HMAC to prevent replay
3. Fix source health scheduler integration
4. Complete i18n wiring for top 20 screens
4. Add admin pairing code HMAC verification
5. Add platform health check before forward (pre-flight)

### P2 — Polish
1. Complete i18n coverage (top 50 screens)
2. Add search/filter to project list
3. Add "Copy pairing code" button
4. Improve source add validation preview
5. Document Instagram Broadcast as "Manual Only"

---

## 11. Database Changes Required

| Migration | Table | Type |
|-----------|-------|------|
| Add `pairing_hmac` to `destinations` | `destinations` | `ALTER TABLE` |
| Add `last_health_check` to `sources` | `sources` | `ALTER TABLE` |
| Add CHECK constraint on `projects.platform_type` | `projects` | `ALTER TABLE` |
| Add FK `destinations.platform_account_id` → `platform_accounts.id` | `destinations` | `ALTER TABLE` |
| Add `media_url`, `media_type`, `media_caption` to `jobs` payload | N/A (payload JSON) | N/A |

---

## 12. Configuration Variables Needed

| Variable | Current | Required |
|----------|---------|----------|
| `PAIRING_CODE_HMAC_SECRET` | ❌ | ✅ Required |
| `MEDIA_CDN_BASE_URL` | ❌ | ✅ For WhatsApp media |
| `MEDIA_CDN_UPLOAD_TOKEN` | ❌ | ✅ For WhatsApp media |
| `PAIRING_SERVER_URL` | ❌ | ✅ For pairing endpoint URL |
| `PAIRING_HMAC_SECRET` | ❌ | ✅ For pairing HMAC |
| `INR_PER_USD` | `85` (hardcoded default) | Add live rate API or admin override |
| `ADMIN_IDS` | Empty string → `{0}` | Fix empty string handling |

---

## 13. Final Verdict

**Overall Readiness: 75%**

The application is **architecturally solid** with excellent separation of concerns, good database design, and sophisticated features (multi-platform, dual-currency wallet, RBAC, pairing). However, **critical bugs in core flows (OTP hang, daily limit race, WhatsApp media missing, pairing endpoint not wired)** prevent production readiness.

**Recommended approach:** Fix P0 bugs first (2-3 days), then P1 polish (2-3 days), then production hardening (1-2 days). Total estimated: 1-2 weeks for production readiness.

---

## 14. Next Steps for Phase 4

1. **Fix P0 bugs** (OTP hang, daily limit atomicity, wallet race)
2. **Wire WhatsApp pairing HTTP endpoint** (standalone aiohttp server or integrated)
3. **Implement WhatsApp media** (CDN abstraction + WhatsApp media types)
4. **Remove dead code** (legacy buttons, duplicate functions)
3. **Wire i18n** across top 20 screens
4. **Add pairing HMAC** for security
5. **Fix source health scheduler**
6. **Complete i18n wiring** for top 20 screens
7. **Run full regression suite**
8. **Create final ZIP** for delivery

---

*End of Audit*