"""Shared test bootstrap: env must be configured BEFORE any project
module import (config.py raises without BOT_TOKEN etc.)."""
import os
import tempfile

_TMP_DB = os.path.join(tempfile.gettempdir(), "channelflow_tests_shared.db")

os.environ.setdefault("DB_NAME", _TMP_DB)
os.environ.setdefault("BOT_TOKEN", "123456:TEST-DUMMY-TOKEN-NOT-REAL")
os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "nHy_k0jrQ60JcRQc981kVFLxpMNTtR_hWve-qVs77Q0=")


def reset_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_TMP_DB + suffix)
        except FileNotFoundError:
            pass
    import database.db as dbmod
    dbmod.init_db()
    return _TMP_DB
