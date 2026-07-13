from contextlib import contextmanager

from src.api import auth as auth_module
from src.api.auth import SqliteSessionStore
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


def test_is_active_uses_read_only_select(tmp_path, monkeypatch):
    db_path = str(tmp_path / "app.sqlite3")
    store = SqliteSessionStore(db_path)
    store.create("valid-session", "credential-fingerprint", 100, 500)
    statements = []
    read_only_modes = []
    real_sqlite_connection = auth_module.sqlite_connection

    @contextmanager
    def traced_connection(path=None, *, read_only=False):
        read_only_modes.append(read_only)
        with real_sqlite_connection(path, read_only=read_only) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(auth_module, "sqlite_connection", traced_connection)

    assert store.is_active("valid-session", "credential-fingerprint", 200) is True

    normalized = [statement.strip().upper() for statement in statements]
    assert read_only_modes == [True]
    assert any(
        statement.startswith("SELECT CREDENTIAL_FINGERPRINT")
        for statement in normalized
    )
    assert any("EXPIRES_AT >" in statement for statement in normalized)
    assert not any(
        statement.startswith(("BEGIN", "COMMIT", "DELETE", "INSERT", "UPDATE"))
        for statement in normalized
    )


def test_expired_session_is_rejected_without_read_path_cleanup(tmp_path):
    db_path = str(tmp_path / "app.sqlite3")
    store = SqliteSessionStore(db_path)
    store.create("expired-session", "credential-fingerprint", 100, 150)

    assert store.is_active("expired-session", "credential-fingerprint", 200) is False

    with sqlite_connection(db_path, read_only=True) as conn:
        row = conn.execute(
            "SELECT session_id FROM auth_sessions WHERE session_id = ?",
            ("expired-session",),
        ).fetchone()
    assert row is not None


def test_creating_session_purges_expired_rows(tmp_path):
    db_path = str(tmp_path / "app.sqlite3")
    store = SqliteSessionStore(db_path)
    store.create("expired-session", "credential-fingerprint", 100, 150)

    store.create("new-session", "credential-fingerprint", 200, 500)

    with sqlite_connection(db_path, read_only=True) as conn:
        expired = conn.execute(
            "SELECT session_id FROM auth_sessions WHERE session_id = ?",
            ("expired-session",),
        ).fetchone()
        active = conn.execute(
            "SELECT session_id FROM auth_sessions WHERE session_id = ?",
            ("new-session",),
        ).fetchone()
    assert expired is None
    assert active is not None
