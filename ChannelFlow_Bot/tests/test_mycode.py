"""OTP input-format unit tests (PRD section 5.2/5.3)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest_shared import reset_db  # noqa: E402

import pytest  # noqa: E402
from core import user_sessions as us


def _fresh_module():
    reset_db()
    return us


# ---- extract_otp_from_command ----------------------------------------

def test_mycode_concatenated():
    ok, code = us.extract_otp_from_command("/mycode94563")
    assert ok is True and code == "94563"


def test_mycode_spaced():
    ok, code = us.extract_otp_from_command("/mycode 94563")
    assert ok is True and code == "94563"


def test_mycode_multi_space():
    ok, code = us.extract_otp_from_command("/mycode   94563")
    assert ok is True and code == "94563"


def test_legacy_myflow_still_accepted():
    ok, code = us.extract_otp_from_command("/myflow94563")
    assert ok is True and code == "94563"


def test_bare_number_is_not_a_code():
    ok, msg = us.extract_otp_from_command("94563")
    assert ok is False
    assert "/mycode" in msg


def test_plain_text_is_not_a_code():
    ok, _ = us.extract_otp_from_command("hello there")
    assert ok is False


def test_non_digit_remainder_rejected():
    ok, _ = us.extract_otp_from_command("/mycode12ab")
    assert ok is False


def test_too_long_rejected():
    ok, _ = us.extract_otp_from_command("/mycode123456789")
    assert ok is False


def test_empty_rejected():
    ok, _ = us.extract_otp_from_command("")
    assert ok is False


def test_extract_never_logs_code(caplog):
    with caplog.at_level("DEBUG"):
        us.extract_otp_from_command("/mycode99999")
    assert "99999" not in caplog.text
