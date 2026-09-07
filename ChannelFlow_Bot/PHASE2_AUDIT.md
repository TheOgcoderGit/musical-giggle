# ChannelFlow AI — Phase 2 Audit

Date: 28 Aug 2026
Scope: Full audit of the Phase 1 final codebase prior to Phase 2 implementation.

## Verified working features
- 4-button main menu (New Project / Home / My Projects / Settings).
- Project CRUD, edit hub, sources, destinations, clone (Telegram part), delete-with-confirm.
- Platform-specific project creation (Telegram→TG/WA/Threads/Instagram).
- Real support ticket system (user + admin, DB-backed, full lifecycle).
- i18n engine + language persistence (7 langs, only ~2 strings routed).
- Wallet (INR ledger), payment request lifecycle, crypto provider polling, coupon + giveaway services.
- Referral capture + reward-on-conversion.
- Admin RBAC panel (`adm:*`) with per-action permission checks + audit logging.
- Forwarder routing correctly scoped per owner/project; no global WhatsApp client.
- Dedup service, daily usage counters, platform-account health.
- Multi-user isolation in handler layer (`_get_owned_project/source/destination`).

## Broken / high-priority bugs found
1. **Owner = admin split-brain.** `is_owner()` is just `id in ADMIN_IDS`; there is no separate owner auth. Legacy admin commands (`/payments`, `/setplan`, `/setprice`, `/setduration`, `/post`, `/broadcast`, maintenance) gate on raw `ADMIN_IDS`, bypassing the DB RBAC entirely. Some declared permissions (`wallet.view`, `wallet.adjust`, `projects.view/manage`, `plans.view/edit`, `broadcast.send`) are NEVER enforced.
2. **Legacy payment approve/reject (`upgrade:approve/reject`)** bypasses RBAC and audit logs.
3. **No owner 2-step auth, no session expiry, no rate-limit/lockout** for admin/owner actions.
4. **Wallet is effectively single-currency.** `wallet_balance_usd` is dead (never written); `get_balance_usd` derives from INR/85. No per-forward billing exists at all.
5. **Crypto wallet top-up bug:** `approve_crypto_payment` does not branch on `purpose='wallet_topup'`, so a crypto top-up would wrongly activate a plan.
6. **Wallet credit/debit lack explicit DB transactions** (autocommit per statement) → balance/ledger desync risk.
7. **UPI `approve_payment` lacks `rowcount` idempotency guard** → double-credit race.
8. **Clone drops `platform_account_id`** for WA/Threads destinations → cloned external destinations are broken (dead-letter `AUTHENTICATION`).
9. **External (WA/Threads) publishes bypass the per-project daily forward limit** (`within_daily_forward_limit` only checked for Telegram dispatch).
10. **Coupon redemption recorded before payment approval** → consumed on abandoned requests.
11. **Giveaway subscription re-delivery can double-grant** a plan.
12. **Hardcoded FX rate 85** scattered across 5 files.
13. **No WhatsApp project-specific pairing identifier** exposed in UI (spec §9). The Cloud-API integration is correct for ownership but has no project-scoped pairing code/token/verification status UI.
14. **No per-forward billing / REFUNDED status** (spec §12-13, §19).
15. **Destructive actions without confirmation:** `settings:disconnect` (Telegram session delete), Instagram destination delete, maintenance toggle.
16. **Fake/dead UI:** "🔔 Notifications" screen has no toggles; `acct:noop` button labeled connected Telegram does nothing; `referral_keyboard` defined but unused; `settings:refresh` edits a never-sent message; legacy `acct:support` "Contact @ChannelFlowSupport_bot" placeholder duplicates real support.
17. **Legacy dead code in `menu_handler`** with a `BTN_ACCOUNT` branch referencing undefined `user_id` (NameError if triggered) and duplicated branches.
18. **Source-channel health monitoring absent** (only platform-account token health exists).
19. **FAQ hardcoded** instead of using the seeded `knowledge_articles` table.
20. **No referral leaderboard / milestones** UI.

## Security concerns
- Admin/owner auth is a single env-set membership check; no 2FA, expiry, lockout.
- Audit log swallows write errors silently; no immutable-enforcement for normal admins (only by convention).
- `admin_user_search` path lacks `users.view` re-check.
- Owner can be banned by an admin with `users.ban` (ineffective but mutates status).
- No secrets logged; `.env` gitignored. OK.

## Database concerns
- No migration versioning (additive checks only) — acceptable for now.
- `wallet_balance_usd` dead column.
- No CHECK constraints on status / direction / purpose enums.
- `admin_notes` added lazily via ALTER at runtime (untracked).
- `plan_durations` backfill is now idempotent (fixed in Phase 1) but plan names are BEGINNER/PRO/CREATOR (Phase 2 spec calls them STARTER/PRO/CREATOR — keep internal names, just relabel in UI to avoid data migration).

## UX problems
- Account/Help/Status/wallet/referrals reachable only via Settings or legacy aliases.
- i18n engine present but barely wired.
- Notifications screen is a fake.
- Back-navigation slightly inconsistent (Account hub dumps to Home instead of Settings).
- Language picker always shows "Current: English".

## Recommended Phase 2 fixes (prioritized)
**P0 (reliability/security/payment correctness)**
- F1: Unify admin/owner auth — add a real owner multi-step auth flow (`/owner`) with env-configured username/password/security answer, rate-limit + lockout + session expiry.
- F2: Enforce RBAC on legacy admin commands (`/payments`, `/setplan`, `/setprice`, `/setduration`, `/post`, `/broadcast`, maintenance) and add the missing permission gates; route legacy payment approve/reject through audited RBAC.
- F3: Fix crypto top-up → wallet (purpose branch).
- F4: Make wallet credit/debit atomic (explicit transaction); add `rowcount` idempotency to UPI approve.
- F5: Fix coupon redemption timing (record only after approval).
- F6: Fix giveaway subscription re-delivery idempotency.

**P1 (core UX/navigation/project mgmt)**
- F7: Clean legacy dead code in `menu_handler`; fix NameError.
- F8: Add confirmation to `settings:disconnect`, IG-destination delete, maintenance toggle, payment reject.
- F9: Remove fake "Notifications" screen or make it functional (mute categories — optional); fix `acct:noop` fake button.
- F10: Make Settings the single navigation hub containing Account/Plan&Wallet/Status/Language/Referrals/Help/Feedback; wire real i18n strings gradually.
- F11: Fix clone to copy `platform_account_id` + re-validate external destination bindings.

**P2 (monetization/referrals/wallet)**
- F12: Per-forward billing (configurable INR/USD) charged post-success with unique job id; prevent double-charge.
- F13: Dual-currency wallet — make USD a real, separate balance where applicable (or document single-currency and add `forward_charge` ledger type).
- F14: Referral leaderboard + milestones (configurable), reward-once.
- F15: Use seeded `knowledge_articles` for FAQ.

**P3 (automation/AI)**
- F16: WhatsApp project-specific pairing identifier + verification status UI (Telegram→WhatsApp only).
- F17: Enforce daily forward limit on external (WA/Threads) publishes.
- F18: Source-channel health monitoring + notify.
- F19: Analytics dashboard (user + admin) from existing stats_service data.

**P4 (polish/extensibility)**
- F20: Plan-relabel STARTER→BEGINNER in UI only; consistent upgrade flow for locked features.
- F21: Centralize FX rate in config/settings.
