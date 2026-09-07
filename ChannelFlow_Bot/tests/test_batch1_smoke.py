"""Batch 1 tests. Run from ChannelFlow_Bot/ (or repo root with the
project dir on sys.path):
    python -m pytest tests/test_batch1_*.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest_shared import reset_db  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    reset_db()
    yield


def test_batch1_placeholder():
    assert True
