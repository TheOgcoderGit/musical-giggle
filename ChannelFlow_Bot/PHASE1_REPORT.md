# ChannelFlow AI — Phase 1 Report (Core Stability, Navigation & Bug-Fixing)

Date: 28 Aug 2026

## PHASE 1 STATUS
- **Implemented**: Yes (Phase 1 scope only)
- **Fixed**: Yes (see Bug Log below)
- **Tested**: Yes (see Testing Evidence)
- **Remaining issues**: See "Remaining known issues"

---

## 1. Bugs found

### Critical / user-visible
1. **`nav:help` callback produced but never handled.** Every "❓ Help & Support" button in Settings and every "⬅ Back to Help" button returned "⚠ Unknown Action".
2. **`destination:wa` / `destination:th` callbacks produced but never handled.** A user with a connected WhatsApp/Threads account could never add that platform as a destination. Since a `whatsapp_channel`/`threads` project needs a destination to start, the project was permanently stuck.
3. **OTP / connect flow stuck after login error.** `WAITING_CONNECT_STAGE` was not cleared on `ConnectError` paths in `_handle_connect_code_or_password`, so after an invalid/expired OTP (or any `ConnectError`), every subsequent message was routed back into the dead connect flow → infinite loop (only `/cancel` escaped).
4. **Payment requests never reaching the admin section.** Admin payment queue only queried `status='SUBMITTED'`, but a request only reaches `SUBMITTED` after the user taps Verify **and** sends a screenshot. Requests stuck in `PENDING_PAYMENT` (user paid but didn't finish the manual verify step) were invisible to admins everywhere.
5. **Admin payment queue crashed on render.** `_render_payment_queue` double-wrapped the tabs markup (`InlineKeyboardMarkup(_queue_tabs(...))`) which raised `ValueError` every time the queue was opened, and the empty state spread a Markup object instead of its rows. The queue was effectively dead even for `SUBMITTED` rows.
6. **`admin:payments` callback dead.** The "💳 Payments" button in the admin dashboard returned "⚠ Unknown Admin Action".
7. **`destinations` table missing `is_middle` / `platform_account_id` columns in existing databases.** `destination_service.add_destination()` inserted those columns and crashed with `OperationalError: table destinations has no column named platform_account_id` on any pre-existing DB.
8. **`plan_durations` missing `CREATOR` rows on existing databases.** The seed guard only ran when the table was empty, so databases seeded before CREATOR existed never got CREATOR durations → "Invalid duration: 1 months for plan CREATOR" for every CREATOR purchase/renewal.

### Medium / polish
9. **`acct:noconn` callback dead** — tapping the Telegram connection row did nothing.
10. **`nav:home` inline button did not render the home dashboard** — it only posted a generic "🏠 Main Menu" reply.
11. **`PENDING_PROJECT_NAME` / `WAITING_PAYMENT_SCREENSHOT` not cleared by `_reset_waiting_states`** — stale state could linger after navigating away from platform-selection or a payment-screenshot prompt.
12. **Unreachable duplicate code** in `_handle_platform_selected` (a full duplicate `create_project` block after an unconditional `return`).
13. **Approved/Rejected admin queue tabs were hardcoded to `SUBMITTED`** — clicking Approved/Rejected always showed the pending rows.

---

## 2. Root causes

| # | Root cause |
|---|-----------|
| 1 | `nav` branch in `button_handler` had no `sub == "help"` case. |
| 2 | Destination platform buttons emitted `destination:wa/th` but only `destinationp` was handled. |
| 3 | Error handling in the connect flow returned without popping `WAITING_CONNECT_STAGE`. |
| 4 | Admin visibility gated exclusively on `SUBMITTED`; the only transition into `SUBMITTED` is a fragile manual two-step (Verify + screenshot) with no catch-up. |
| 5 | `_queue_tabs()` already returns a `Markup`, but the caller wrapped it again; empty-state used `*Markup`. |
| 6 | `admin:` branch handled only `refresh`/`broadcast`/`maintenance`. |
| 7 | Migrations existed for other columns but `is_middle`/`platform_account_id` were never added by `_migrate_existing_schema`. |
| 8 | Seed logic only inserted when the whole table was empty (all-or-nothing), so a partially-seeded table stayed incomplete forever. |

---

## 3. Fixes applied

1. Added `sub == "help"` case to the `nav` branch → renders `help_section_keyboard`. (`bot/handlers.py`)
2. Changed `destination:wa:{id}` → `destinationp:wa:{id}` and `destination:th:{id}` → `destinationp:th:{id}`. (`bot/handlers.py`)
3. Cleared `WAITING_CONNECT_STAGE` on both `ConnectError` paths and gave the user a `/connect <phone>` retry hint. (`bot/handlers.py`)
4. Admin pending queue now queries `status IN ('PENDING_PAYMENT','SUBMITTED')` for the pending tabs; rows without proof are shown as "no proof yet" (not actionable), `SUBMITTED` rows keep approve/reject. (`bot/admin_panel.py`)
5. Fixed the double-wrapped markup and the empty-state row handling in `_render_payment_queue`. (`bot/admin_panel.py`)
6. Wired `admin:payments` → `_render_finance`. (`bot/handlers.py`)
7. Added `is_middle` + `platform_account_id` migrations in `_migrate_existing_schema`. (`database/db.py`)
8. Changed the `plan_durations` seed to idempotently backfill missing (plan, months) rows for BEGINNER/PRO/CREATOR without touching existing rows. (`database/db.py`)
9. Changed `acct:noconn` → `acct:noop`. (`bot/keyboards.py`)
10. `nav:home` now renders the real home dashboard via `_send_home`. (`bot/handlers.py`)
11. Added `PENDING_PROJECT_NAME` + `WAITING_PAYMENT_SCREENSHOT` to `_reset_waiting_states`. (`bot/handlers.py`)
12. Removed the unreachable duplicate block in `_handle_platform_selected`. (`bot/handlers.py`)
13. `_render_payment_queue` now honors the requested tab status (APPROVED/REJECTED) instead of hardcoding `SUBMITTED`. (`bot/admin_panel.py`)

---

## 4. Files changed

- `bot/handlers.py`
- `bot/keyboards.py`
- `bot/admin_panel.py`
- `database/db.py`

---

## 5. Database changes

- **Migration**: `destinations` gains `is_middle INTEGER NOT NULL DEFAULT 0` and `platform_account_id INTEGER` on existing databases (idempotent, guarded by `_column_exists`).
- **Seed backfill**: `plan_durations` gains any missing (plan, months) rows for BEGINNER/PRO/CREATOR (idempotent, per-row check).
- No destructive schema changes; `init_db()` remains safe to re-run.

---

## 6. New tests added

No formal test framework exists in this project, so tests were run as inline integration scripts against the real code paths:

- Module import test (`config`, `database.db`, `bot.keyboards`, `bot.states`, `bot.handlers`, `bot.admin_panel`, `services.payment_service`).
- DB integration test: user registration, project create/list/delete, source add/list, destination add/list, cross-user isolation, payment create → screenshot submit → approve, wallet balance + auto-renew.
- Admin payment queue render test: pending (PENDING_PAYMENT + SUBMITTED), approved, rejected/empty tabs.

---

## 7. Tests passed

- ✅ Compile (`py_compile`) of every modified file.
- ✅ Full module import.
- ✅ DB init + migrations applied to the existing `channelflow.db` (verified `is_middle`, `platform_account_id`, CREATOR durations now present).
- ✅ Project CRUD, source CRUD, destination CRUD.
- ✅ Multi-user isolation (user A's projects invisible to user B).
- ✅ Payment lifecycle: PENDING_PAYMENT → SUBMITTED → APPROVED.
- ✅ Admin queue renders PENDING / APPROVED / REJECTED / empty tabs without crashing.
- ✅ Wallet balance + auto-renew toggling.

---

## 8. Tests that could not be executed and why

- **Real Telegram API tests** (/start, button taps, OTP submission, real forwarding): no real Telegram bot token / API credentials were used to avoid touching live accounts. The existing `ChannelFlow.session` file is incompatible with Telethon 1.41.2 in this sandbox, and no live network bot was started.
- **Live forwarding engine** (Telethon client): requires a real authorized `.session`; not run.
- **Real UPI / crypto payment provider** (Oxapay webhook): requires provider credentials.
- These are marked **not executed**; all DB-level and logic-level behavior was verified locally.

---

## 9. Existing features verified

- Main menu (4 top-level buttons: New Project / My Projects / Home / Settings) intact.
- Project management (card view, edit hub, sources, destinations, filters, settings, AI, watermark, formatting, delete-with-confirm) intact and reachable after project creation.
- Platform-specific project creation (Telegram / WhatsApp Channel / Threads / locked tiers) intact.
- Home dashboard and Settings hub intact.
- Payment method-first flow (UPI / crypto), wallet, plan & billing intact.
- Connect/OTP flow logic (phone → code → password → finalize) preserved; stuck-state removed.
- Admin panel (`adm:*`) and owner `/admin` dashboard intact.

---

## 10. Remaining known issues

- `settings:systatus` handler exists but has no button (harmless dead handler; left as-is to avoid scope creep).
- `projlist` callback is handled but never produced (harmless alias).
- Legacy `BTN_ACCOUNT`/`BTN_STATUS` duplicate branches in `menu_handler` are unreachable dead code (left in place; they are defensive aliases).
- `WAITING_PAYMENT_SCREENSHOT` has no TTL timeout on its own (it is cleared by `_reset_waiting_states` and on submit/cancel).
- Real Telegram / payment-provider paths could not be exercised in this sandbox (see section 8).
- `channelflow.db` in the workspace had migrations applied to it locally; a fresh deploy re-applies them automatically from `database/db.py`.
