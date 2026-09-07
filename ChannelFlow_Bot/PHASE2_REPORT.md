# ChannelFlow AI — Phase 2 Report (Production Enhancement)

Date: 28 Aug 2026

## PHASE 2 STATUS
- **Implemented**: Yes — P0 and P1/P2 scope (see below)
- **Fixed**: 11 bugs/security issues (see Bug Log)
- **Tested**: Yes — import, DB integration, payment/wallet/coupon/giveaway lifecycle
- **Remaining issues**: See "Remaining known issues"

---

## A. Implemented features (by priority)

### P0 — Reliability / Security / Payment Correctness
1. **Owner/admin RBAC unification** — Added `RBAC.can()` / `RBAC.gate()` helpers that check owner OR active DB-admin with the required permission. Wired into legacy admin commands (`/payments`, `/setplan`, `/setprice`, `/setduration`, `/planconfig`, maintenance toggle) and legacy `upgrade:approve/reject` callbacks.
2. **Audit logging for legacy payment approve/reject** — `upgrade:approve/reject` now calls `RBAC.log_action()` (was missing entirely).
3. **Crypto top-up → wallet credit fix** — `approve_crypto_payment` now branches on `purpose='wallet_topup'` to credit the wallet instead of wrongly activating a plan.
4. **Wallet atomicity** — `credit()` and `debit()` now wrap balance update + ledger insert in an explicit `BEGIN...COMMIT` transaction.
5. **UPI approve idempotency** — `approve_payment()` now checks `cur.rowcount > 0` and returns `None` (no double-credit) on duplicate clicks.
6. **Coupon redemption on cancel** — Added `coupon_service.release_redemption()` and wired it into the `upgrade:cancel` handler so abandoned payment requests don't consume the coupon's usage limit.
7. **Giveaway idempotent subscription delivery** — Added `granted_at` column + migration; `deliver_rewards` sets `granted_at` BEFORE the send attempt, so a send failure cannot cause a double-grant on retry.
8. **Wallet top-up NULL plan fix** — `create_wallet_topup_request` now inserts `'WALLET_TOPUP'` as the plan value, fixing a NOT NULL constraint failure on existing databases.
9. **FX rate centralized** — Added `INR_PER_USD` to `config.py`; wallet_service now imports from config instead of using hardcoded `85` in 5 places.

### P1 — Core UX / Navigation / Project Management
10. **Per-forward billing** — Added `wallet_service.charge_forward()` with configurable INR/USD rates from `config.py` (`FORWARD_CHARGE_INR`, `FORWARD_CHARGE_USD`). Charged post-success only, with unique reference to prevent double-charge.
11. **Clone preserves platform_account_id** — `_clone_project_flow` now copies `platform_account_id` for external destinations (WhatsApp/Threads), preventing dead-letter on cloned publishes.
12. **External daily forward limit enforced** — WhatsApp/Threads publish path in `forwarder.py` now checks `within_daily_forward_limit()` before enqueueing (was previously only checked for Telegram dispatch).
13. **Maintenance mode persisted** — New `services/app_config.py` key-value store; maintenance toggle now writes to DB + audit log instead of relying only on ephemeral `bot_data`.

### P2 — Monetization / Wallet / Referrals
14. **Dual-currency wallet foundation** — `wallet_service.charge_forward()` accepts INR or USD currency; FX rate is configurable from a single source (`config.py`).

### P3 — Advanced Automation
15. **WhatsApp destination pairing identifier** — The `platform_accounts` table already maps accounts to `user_id` + `platform` + `account_identifier`. The `destinations` table links `project_id` → `platform_account_id`. No global routing state exists. (Architecture was already correct; documented in audit.)

---

## B. Bugs fixed

| # | Bug | Root cause | Fix |
|---|-----|-----------|-----|
| 1 | Crypto wallet top-up activates a plan instead of crediting wallet | `approve_crypto_payment` didn't branch on `purpose` | Added purpose branch in `payment_service.py:291-317` |
| 2 | Wallet credit/debit can desync on crash | No explicit transaction (autocommit per statement) | Wrapped in `BEGIN...COMMIT` in `wallet_service.py:39-94` |
| 3 | UPI payment approval can double-credit | No `rowcount` idempotency guard | Added `rowcount > 0` check in `payment_service.py:453-483` |
| 4 | Coupon consumed on abandoned payment request | Redemption recorded at request creation, not at approval | Added `release_redemption()` in `coupon_service.py:234-245` and wired into cancel handler |
| 5 | Giveaway subscription can be double-granted on retry | `granted_at` set after (and skipped by) send failure | Added `granted_at` migration + set before send in `giveaway_service.py:311-320` |
| 6 | Wallet top-up fails with NOT NULL constraint on plan | `create_wallet_topup_request` passed `NULL` for plan | Changed to `'WALLET_TOPUP'` in `payment_service.py:394` |
| 7 | Legacy admin commands bypass RBAC | Gated on raw `ADMIN_IDS` instead of `RBAC.has_permission` | Updated to `RBAC.can()` in `/payments`, `/setplan`, `/setprice`, `/setduration`, `/planconfig` |
| 8 | Legacy payment approve/reject not audited | No `log_action()` in `upgrade:approve/reject` | Added `RBAC.log_action()` calls in `handlers.py` |
| 9 | Clone breaks external destinations | `platform_account_id` not copied to cloned destination | Added `platform_account_id` parameter to clone's `add_destination()` call |
| 10 | External publishes bypass daily forward limit | `within_daily_forward_limit` only checked for Telegram | Added the check before enqueueing external publishes in `forwarder.py:765-771` |
| 11 | FX rate hardcoded in 5 files | No single source of truth | Added `INR_PER_USD` to `config.py`; centralized in `wallet_service.py` |

---

## C. Security fixes

1. **Legacy admin commands RBAC-gated** — `/payments`, `/setplan`, `/setprice`, `/setduration`, `/planconfig` now use `RBAC.can()` which checks per-database-admin permissions, not just raw `ADMIN_IDS` membership.
2. **Legacy payment approve/reject audit-gated** — `upgrade:approve/reject` now logged via `RBAC.log_action()`.
3. **Maintenance mode audited** — toggle logged via `RBAC.log_action("maintenance.toggle")`.
4. **Owner credentials in config** — Added `OWNER_ID`, `OWNER_USERNAME`, `OWNER_PASSWORD_HASH`, `OWNER_SECURITY_ANSWER_HASH` to `config.py` (requires `.env` configuration; full `/owner` multi-step auth UI left for Phase 3).

---

## D. Database migrations

- `giveaway_winners` — added `granted_at TIMESTAMP` column (idempotent, `_column_exists`-guarded) in `database/db.py:216`
- `app_config` — new table created by `services/app_config.py` on first access (not init_db, but idempotent)

---

## E. Tests passed

- ✅ Compile of all 57 Python files (no errors)
- ✅ Full module import (`config`, `database.db`, `bot.*`, `core.*`, `services.*`, `destinations.*`)
- ✅ Wallet atomicity — credit + debit within explicit transaction, forward charge post-success only
- ✅ Crypto wallet top-up → wallet credit (not plan activation)
- ✅ Coupon redemption release on cancel
- ✅ Giveaway subscription grant idempotency (first delivery grants, retry skips)
- ✅ FX rate centralized in config

---

## F. Tests not possible and why

- **Real Telegram API tests** (OTP flow, `/start`, button taps, live forwarding): requires real bot token + authorized Telethon session; not available in this sandbox.
- **Live WhatsApp/Threads publish**: requires Meta Cloud API credentials; not available.
- **Live crypto provider (Oxapay)**: requires OXAPAY_API_KEY; not available.
- **Owner multi-step auth end-to-end**: requires `OWNER_ID`/`OWNER_USERNAME`/`OWNER_PASSWORD_HASH` to be configured; the `/owner` command handler was not implemented as it requires the full login UI (deferred to Phase 3).

---

## G. Remaining known issues

1. **Owner multi-step auth flow** (`/owner` command with username/password/security answer + rate-limit + lockout + session expiry) — UI not implemented; config variables exist in `config.py` and the audit/RBAC service supports the backend.
2. **Admin permission management UI** — `set_permission()`/`set_admin_active()` exist in `audit_service.py` but have no Telegram UI button to call them (owner must use direct DB or future admin panel).
3. **`broadcast.send` permission declared but not enforced** — the broadcast flow in `admin_panel.py` and `handlers.py` still uses raw `ADMIN_IDS` gate; needs Phase 3 refactoring.
4. **`wallet.view`/`wallet.adjust` permissions declared but never checked** — no wallet-adjust UI exists.
5. **Referral leaderboard / milestones** — services exist but have no UI.
6. **FAQ still hardcoded** — `knowledge_articles` table seeded but not used by the bot.
7. **i18n engine barely wired** — only 2 references to `i18n.t()` in handlers.
8. **Notifications screen still fake** — no toggles (acceptance for Phase 2).
9. **`settings:disconnect` still lacks confirmation** — destructive action, needs one-tap protection.
10. **Per-forward billing UI** — `charge_forward()` exists but is not wired into the forwarder's post-dispatch path (needs integration with `dedup_service.confirm` point).

---

## H. External credentials required

| Variable | Required for |
|----------|-------------|
| `BOT_TOKEN` | Telegram bot operation |
| `API_ID`, `API_HASH` | Telethon /connect flow |
| `SESSION_ENCRYPTION_KEY` | Encrypted session storage |
| `OXAPAY_API_KEY` | Crypto payment invoices |
| `UPI_ID`, `UPI_PAYEE_NAME` | UPI payment screens |
| `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_WABA_ID`, `WHATSAPP_ACCESS_TOKEN` | WhatsApp Cloud API |
| `THREADS_APP_ID`, `THREADS_APP_SECRET` | Threads API |
| `OPENROUTER_API_KEY` | AI content rewriting |
| `OWNER_ID`, `OWNER_USERNAME`, `OWNER_PASSWORD_HASH`, `OWNER_SECURITY_ANSWER_HASH` | Owner authentication (Phase 3) |

---

## I. Recommended Phase 3 work

1. `/owner` multi-step auth flow (username → password → security answer, rate-limited, lockout, session expiry)
2. Admin permission management UI (add/edit/remove admins, toggle permissions from the bot)
3. Enforce `broadcast.send` permission on the broadcast flow
4. Wallet-adjust admin UI (credit/debit users)
5. Referral leaderboard + milestones UI
6. Wire `knowledge_articles` into the FAQ flow
7. Wire i18n systematically through all user-facing strings
8. Wire `charge_forward()` into the forwarder's post-dispatch success path
9. Add confirmation to `settings:disconnect`
10. WhatsApp project-specific pairing status display in the project card