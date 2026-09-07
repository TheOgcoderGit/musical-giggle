"""Hard product rule: NO per-forward billing (PRD sections 2.2, 22).
Wallet charge APIs must remain no-ops; forwarding path never debits."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest_shared import reset_db  # noqa: E402

import pytest  # noqa: E402

from database.models import register_user  # noqa: E402
from services import wallet_service  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    reset_db()


def test_wallet_never_debited_per_forward():
    register_user(777001, "w", "W")
    wallet_service.credit_currency(777001, 100.0, "INR", reference="seed1")
    assert wallet_service.get_balance_inr(777001) == 100.0

    # Forward-path charge calls (whatever survives from the legacy
    # engine) must never reduce the wallet.
    wallet_service.charge_forward(777001)
    wallet_service.reserve_forward_charge(777001, 1)
    wallet_service.confirm_forward_charge(1)
    wallet_service.release_forward_charge(1)
    wallet_service.reserve_forward_charge(777001, 2)
    wallet_service.release_forward_charge(2)

    assert wallet_service.get_balance_inr(777001) == 100.0
    assert wallet_service.get_balance_usd(777001) == 0.0


def test_no_charge_api_calls_in_forwarder():
    """Structural guarantee: core/forwarder.py must not import or call
    per-forward wallet charges."""
    import inspect
    import core.forwarder as fwd
    src = inspect.getsource(fwd)
    for forbidden in ("reserve_forward_charge", "confirm_forward_charge",
                      "charge_forward", "wallet_service"):
        assert forbidden not in src, f"forwarder references {forbidden}"
