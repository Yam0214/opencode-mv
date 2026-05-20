import sqlite3

import pytest


@pytest.fixture(autouse=True)
def isolate_db_env(tmp_path, monkeypatch):
    """Ensure all tests use a temporary database, never the real one."""
    db_path = tmp_path / "opencode.db"
    monkeypatch.setenv("OPENCODE_DB_PATH", str(db_path))
