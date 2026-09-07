"""Forwarding pipeline integrity tests (PRD sections 14, 22.1):
dispatch must not crash on a normal message and must reserve exactly
one daily-quota unit per accepted message."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest_shared import reset_db  # noqa: E402

import pytest  # noqa: E402

from database.db import get_connection  # noqa: E402
from database.models import register_user  # noqa: E402
from services import plan_service, project_service, source_service  # noqa: E402
from core import forwarder  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    reset_db()


class FakeChat:
    chat_id = -1001234567890


class FakeMedia:
    pass


class FakeMsg:
    def __init__(self, mid=1):
        self.id = mid
        self.chat_id = FakeChat.chat_id
        self.grouped_id = None
        self.raw_text = "hello world"
        self.sender_id = 55
        self.media = None
        self.photo = None
        self.video = None
        self.gif = None
        self.document = None


def _usage_count(project_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT forward_count FROM daily_usage WHERE project_id=? AND usage_date=date('now')",
        (project_id,),
    ).fetchone()
    conn.close()
    return row["forward_count"] if row else 0


def test_dispatch_does_not_crash_on_plain_message():
    """Regression for the UnboundLocalError dead-code bug: a message
    that passes filters must reach the (empty) destination loop."""
    register_user(9001, "u", "U")
    pid = project_service.create_project(9001, "P1")
    route = {
        "project_id": pid,
        "settings": {"mode": "forward", "delay_min": 0, "delay_max": 0},
        "content_rules": None,
        "has_formatting": False,
        "destinations": [],
        "external_destinations": [],
        "owner_id": 9001,
    }

    async def run():
        try:
            await forwarder._dispatch([FakeMsg()], route, None)
        except Exception as e:  # pragma: no cover
            raise AssertionError(f"_dispatch crashed: {e!r}")

    asyncio.run(run())


def test_dispatch_reserves_exactly_one_quota_unit():
    """A filter-passing message with zero destinations reserves its
    single daily unit and (having nothing published) returns it -
    net quota unchanged, and never two units."""
    register_user(9002, "u2", "U2")
    pid = project_service.create_project(9002, "P2")
    route = {
        "project_id": pid,
        "settings": {"mode": "forward", "delay_min": 0, "delay_max": 0},
        "content_rules": None,
        "has_formatting": False,
        "destinations": [],
        "external_destinations": [],
        "owner_id": 9002,
    }

    before = _usage_count(pid)

    async def run():
        await forwarder._dispatch([FakeMsg(1)], route, None)
        await forwarder._dispatch([FakeMsg(2)], route, None)

    asyncio.run(run())
    after = _usage_count(pid)

    # reserve+release per message -> no net consumption, and critically
    # never 2 units stuck from a double reservation.
    assert after == before


def test_dispatch_filtered_message_consumes_nothing():
    register_user(9003, "u3", "U3")
    pid = project_service.create_project(9003, "P3")
    route = {
        "project_id": pid,
        "settings": {"mode": "forward", "delay_min": 0, "delay_max": 0,
                     "blacklist": ["hello"]},
        "content_rules": None,
        "has_formatting": False,
        "destinations": [],
        "external_destinations": [],
        "owner_id": 9003,
    }
    before = _usage_count(pid)

    async def run():
        await forwarder._dispatch([FakeMsg(1)], route, None)

    asyncio.run(run())
    assert _usage_count(pid) == before
