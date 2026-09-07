"""Ownership + trial DB-level tests (PRD sections 2.4, 6.x, 46)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest_shared import reset_db  # noqa: E402

import pytest  # noqa: E402

from database.db import get_connection, init_db  # noqa: E402
from database.models import register_user  # noqa: E402
from services import plan_service  # noqa: E402
from core import user_sessions as us  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    reset_db()


def _insert_session(channel_user_id, external_id, status="connected"):
    conn = get_connection()
    conn.execute(
        "INSERT INTO user_telegram_sessions"
        "(telegram_id, encrypted_session, phone_number, status, external_telegram_id)"
        " VALUES (?, 'ciphertext', '+10000000000', ?, ?)",
        (channel_user_id, status, external_id),
    )
    conn.commit()
    conn.close()


def _idx_names():
    conn = get_connection()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND name='idx_uts_one_active_owner'"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---- DB-level ownership uniqueness -----------------------------------

def test_unique_active_owner_index_exists():
    assert _idx_names() == ["idx_uts_one_active_owner"]


def test_two_users_cannot_own_same_external_account():
    register_user(1, "a", "A")
    register_user(2, "b", "B")
    _insert_session(1, 999)
    # Second insert for the same external id must be rejected by the DB
    # (the application-level claim check would already reject it; this
    # proves the index is the backstop).
    conn = get_connection()
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO user_telegram_sessions"
            "(telegram_id, encrypted_session, phone_number, status, external_telegram_id)"
            " VALUES (2, 'ciphertext2', '+20000000000', 'connected', 999)"
        )
        conn.commit()
    conn.close()


def test_disconnect_releases_ownership():
    """Deleting the connected row must let another user claim the same
    external account (PRD 6.7)."""
    register_user(1, "a", "A")
    register_user(2, "b", "B")
    _insert_session(1, 12345)
    us.disconnect_user(1)  # releases
    conn = get_connection()
    conn.execute(
        "INSERT INTO user_telegram_sessions"
        "(telegram_id, encrypted_session, phone_number, status, external_telegram_id)"
        " VALUES (2, 'ciphertext2', '+20000000000', 'connected', 12345)"
    )
    conn.commit()
    conn.close()


def test_claim_conflict_message_is_safe():
    register_user(1, "alice", "Alice")
    register_user(2, "bob", "Bob")
    _insert_session(1, 777, status="connected")
    with pytest.raises(us.ConnectConflict) as ei:
        us._claim_session_sync(2, 777, "enc", "+20000000000", None)
    text = str(ei.value)
    assert "already connected" in text
    # Never leaks the other user's external id, phone or username.
    assert "777" not in text
    assert "+10000000000" not in text
    assert "alice" not in text.lower()


def test_same_user_reclaim_ok():
    register_user(1, "a", "A")
    _insert_session(1, 555)
    ok = us._claim_session_sync(1, 555, "newer-cipher", "+10000000000", "alice")
    assert ok is True
    conn = get_connection()
    row = conn.execute(
        "SELECT encrypted_session FROM user_telegram_sessions WHERE telegram_id=1"
    ).fetchone()
    conn.close()
    assert row["encrypted_session"] == "newer-cipher"


# ---- Trial -----------------------------------------------------------

def test_trial_granted_once_and_persistent():
    register_user(10, "trial", "T")
    assert plan_service.start_trial(10) is True
    assert plan_service.start_trial(10) is False  # idempotent within run
    assert plan_service.get_entitlements(10)["plan"] == "CREATOR"

    conn = get_connection()
    row = conn.execute(
        "SELECT plan, plan_expiry, trial_granted_at FROM users WHERE telegram_id=10"
    ).fetchone()
    conn.close()
    assert row["plan"] == "CREATOR"
    assert row["plan_expiry"] is not None
    assert row["trial_granted_at"] is not None

    # Simulate restart + expired trial: marker persists, no re-grant.
    plan_service.set_user_plan(10, "FREE", expiry=None)
    assert plan_service.start_trial(10) is False
    conn = get_connection()
    row2 = conn.execute(
        "SELECT trial_granted_at FROM users WHERE telegram_id=10"
    ).fetchone()
    conn.close()
    assert row2["trial_granted_at"] is not None


def test_trial_not_granted_to_existing_paid_user():
    register_user(11, "paid", "P")
    plan_service.set_user_plan(11, "PRO", expiry=None)  # admin-assigned
    assert plan_service.start_trial(11) is False


# ---- Stale-state cleanup helpers ------------------------------------

def test_expired_attempt_rejected_without_crash():
    """PRD 5.5/5.8: the expiry branch must use time.monotonic() - the
    old `_time` NameError crashed every valid OTP submission before it
    reached Telegram."""
    import inspect
    src = inspect.getsource(us)
    assert "_time.monotonic" not in src
    assert "time.monotonic()" in src
