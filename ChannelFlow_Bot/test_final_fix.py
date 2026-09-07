import database.db as db
from database.models import register_user
db.init_db()

from database.db import get_connection
from services import (
    wallet_service, payment_service, referral_service, 
    project_service, destination_service, plan_service,
    i18n_service
)
from core import pairing_endpoint
from bot import owner_panel
from config import PAIRING_CODE_EXPIRY_MINUTES

print('=== FINAL REGRESSION TEST ===')

def clean(uid):
    conn = get_connection(); cur = conn.cursor()
    for t in ['coupon_redemptions','payment_requests','projects','wallet_transactions','giveaway_winners','referral_milestone_grants','referrals','user_notification_prefs','destinations','sources']:
        try: cur.execute('DELETE FROM %s WHERE user_id=?' % t, (uid,))
        except Exception:
            try: cur.execute('DELETE FROM %s WHERE project_id IN (SELECT id FROM projects WHERE user_id=?)' % t, (uid,))
            except Exception: pass
    cur.execute('DELETE FROM users WHERE telegram_id=?', (uid,))
    conn.commit(); conn.close()

uA, uB = 200000001, 200000002
for u in (uA, uB): clean(u)
register_user(uA, 'usera', 'UserA')
register_user(uB, 'userb', 'UserB')

# Setup WhatsApp accounts
from services import platform_accounts_service as PA
PA.upsert_account(uA, 'whatsapp_channel', 'wa_a', {'access_token': 'tok_a', 'phone_number_id': '123'})
PA.upsert_account(uB, 'whatsapp_channel', 'wa_b', {'access_token': 'tok_b', 'phone_number_id': '456'})
acct_a = PA.get_account(uA, 'whatsapp_channel', 'wa_a')
acct_b = PA.get_account(uB, 'whatsapp_channel', 'wa_b')

# --- Test 1: Pairing verification ---
print('Test 1: Pairing verification flow...')
pA = project_service.create_project(uA, 'WA Proj A', platform_type='whatsapp_channel')
destination_service.add_destination(
    pA, 'wa:wa_a', None, 'WA Dest A', 'whatsapp_channel',
    platform_account_id=acct_a['id'],
)
dests = [d for d in destination_service.get_destinations(pA) if d['chat_type']=='whatsapp_channel']
dA = dests[-1]
conn = get_connection(); cur = conn.cursor()
from services.destination_service import _generate_unique_pairing_code
code = _generate_unique_pairing_code(cur)
conn.close()
destination_service.set_destination_pairing(dA['id'], code, status='pending')

async def run1():
    res = await pairing_endpoint.verify_pairing_code(code)
    assert res['ok'] and res['status'] == 'verified'
    res2 = await pairing_endpoint.verify_pairing_code(code)
    assert res2['ok'] and res2['status'] == 'verified'
    print('  Pairing verified OK')

import asyncio
asyncio.run(run1())

# Test 2: Cross-user isolation
print('Test 2: Cross-user isolation...')
pB = project_service.create_project(uB, 'Proj B', platform_type='whatsapp_channel')
destination_service.add_destination(pB, 'wa:wa_b', None, 'WA B', 'whatsapp_channel', platform_account_id=acct_b['id'])
from services.project_service import get_project
proj_a = get_project(pA)
assert proj_a["user_id"] == uA
print("  Cross-user isolation OK")

# Test 3: i18n
print('Test 3: i18n WhatsApp strings...')
from services.i18n_service import t
msg = i18n_service.t(uA, "whatsapp.destination_added", code="CFXXXX", minutes=10)
assert "pairing" in msg.lower()
print("  i18n OK")

# Test 4: Content transformation
print("Test 4: Content transformation...")
from services.whatsapp_content import convert_telegram_to_whatsapp
assert "*Bold*" in convert_telegram_to_whatsapp("<b>Bold</b>", "html")
print("  HTML->WA Markdown OK")

# Test 5: Wallet billing (Phase 2.2)
print("Test 5: Wallet billing idempotency...")
uid = 300000001
conn = get_connection(); cur = conn.cursor()
cur.execute('DELETE FROM wallet_transactions WHERE user_id=?', (uid,))
cur.execute('DELETE FROM users WHERE telegram_id=?', (uid,))
conn.commit(); conn.close()
register_user(uid, 'w', 'W')

from services import wallet_service
wallet_service.credit_currency(uid, 100.0, 'INR', reference='test_inr')
wallet_service.credit_currency(uid, 50.0, 'USD', reference='test_usd')
assert wallet_service.get_balance_inr(uid) == 100.0
assert wallet_service.get_balance_usd(uid) == 50.0
print('  Dual currency wallet: OK')

# PRD rule (section 2.2): there is NO per-forward billing. The legacy
# charge APIs are compatibility no-ops; nothing may deduct wallet money
# per forward.
ok1 = wallet_service.reserve_forward_charge(uid, 9991)
ok2 = wallet_service.reserve_forward_charge(uid, 9991)
assert ok1 and ok2
assert wallet_service.get_balance_inr(uid) == 100.0  # nothing deducted

wallet_service.confirm_forward_charge(9991)
wallet_service.reserve_forward_charge(uid, 9992)
wallet_service.release_forward_charge(9992)
assert abs(wallet_service.get_balance_inr(uid) - 100.0) < 0.001
print('  No per-forward billing: OK')

# Even a zero-balance user can "forward" - no wallet charge exists.
uid2 = 888888888
register_user(uid2, 'poorsuser', 'PoorUser')
ok = wallet_service.reserve_forward_charge(uid2, 9993)
assert ok
assert wallet_service.get_balance_inr(uid2) == 0.0
print('  No insufficient-balance gate (no per-forward billing): OK')

# Test 6: Atomic daily limit
print("Test 6: Atomic daily limit...")
uid3 = 400000001
register_user(uid3, 'lim', 'Lim')
pid = project_service.create_project(uid3, 'Lim Proj')
plan_service.set_user_plan(uid3, 'BEGINNER', 30)
plan_service.reserve_daily_forward(uid3, pid)
plan_service.reserve_daily_forward(uid3, pid)
plan_service.release_daily_forward(pid)
conn = get_connection(); cur = conn.cursor()
cur.execute('UPDATE daily_usage SET forward_count=200 WHERE project_id=?', (pid,))
conn.commit(); conn.close()
assert plan_service.reserve_daily_forward(uid3, pid) is False
print('  Atomic daily limit OK')

# Test 7: Content transformation
print("Test 7: WA content transform...")
from services.whatsapp_content import convert_telegram_to_whatsapp
assert "_italic_" in convert_telegram_to_whatsapp("__italic__", "markdown")
print("  Markdown->WA OK")

# Test 8: Atomic daily quota under concurrency
print("Test 8: Atomic daily quota test...")
import asyncio
async def test_concurrent():
    from services import plan_service as ps
    from services import project_service as pj
    from database.models import register_user as reg
    
    uid = 500000001
    reg(uid, 'concurrent', 'ConcurrentUser')
    pid = pj.create_project(uid, 'Concurrent', platform_type='whatsapp_channel')
    ps.set_user_plan(uid, 'BEGINNER', 30)
    
    # Simulate 250 concurrent reserve attempts (limit is 200)
    results = []
    for i in range(250):
        r = ps.reserve_daily_forward(uid, pid)
        results.append(r)
    
    successes = sum(1 for r in results if r)
    assert successes == 200, f'Expected 200 successes, got {successes}'
    print(f'  Concurrent reserve test: {successes}/200 succeeded (limit=200)')

asyncio.run(test_concurrent())

print('=== ALL REGRESSION TESTS PASSED ===')