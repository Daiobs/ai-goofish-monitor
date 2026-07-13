import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect

import src.app as app_module
from src.api import auth as auth_module
from src.api.auth import (
    InMemorySessionStore,
    SESSION_COOKIE_NAME,
    SessionManager,
)
from src.api.routes import accounts


TEST_USERNAME = "security-test-admin"
TEST_PASSWORD = "security-test-password"
TEST_SESSION_SECRET = "security-test-session-secret-0123456789-ABCDEF"


@pytest.fixture()
def auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    monkeypatch.setattr(app_module.app_settings, "app_env", "development")
    monkeypatch.setattr(app_module.app_settings, "web_username", TEST_USERNAME)
    monkeypatch.setattr(app_module.app_settings, "web_password", TEST_PASSWORD)
    monkeypatch.setattr(
        app_module.app_settings,
        "session_secret",
        TEST_SESSION_SECRET,
    )
    monkeypatch.setattr(app_module.app_settings, "session_max_age_seconds", 3600)
    monkeypatch.setattr(app_module.app_settings, "session_cookie_secure", False)
    monkeypatch.setattr(accounts, "_state_dir", lambda: str(tmp_path))
    auth_module.reset_session_manager()

    client = TestClient(app_module.app)
    yield client, tmp_path

    client.close()
    auth_module.reset_session_manager()


def _login(client: TestClient):
    return client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/tasks",
        "/api/accounts",
        "/api/results/files",
        "/api/settings/status",
        "/api/prompts",
        "/api/logs?task_id=0",
    ],
)
def test_sensitive_api_routes_require_authentication(auth_client, path):
    client, _ = auth_client

    response = client.get(path)

    assert response.status_code == 401


def test_health_and_session_status_remain_anonymous(auth_client):
    client, _ = auth_client

    health_response = client.get("/health")
    session_response = client.get("/auth/session")

    assert health_response.status_code == 200
    assert session_response.status_code == 200
    assert session_response.json() == {"authenticated": False}


def test_unauthenticated_account_read_does_not_leak_file_content(auth_client):
    client, state_dir = auth_client
    sensitive_content = '{"cookies":[{"value":"account-secret"}]}'
    (state_dir / "primary.json").write_text(sensitive_content, encoding="utf-8")

    response = client.get("/api/accounts/primary")

    assert response.status_code == 401
    assert sensitive_content not in response.text
    assert "account-secret" not in response.text


def test_login_sets_hardened_cookie_and_allows_protected_access(auth_client):
    client, _ = auth_client

    login_response = _login(client)

    assert login_response.status_code == 200
    assert login_response.json() == {
        "authenticated": True,
        "username": TEST_USERNAME,
    }
    cookie_header = login_response.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE_NAME}=" in cookie_header
    assert "httponly" in cookie_header
    assert "samesite=strict" in cookie_header
    assert "path=/" in cookie_header
    assert "max-age=3600" in cookie_header
    assert "secure" not in cookie_header

    session_response = client.get("/auth/session")
    protected_response = client.get("/api/accounts")

    assert session_response.json() == {
        "authenticated": True,
        "username": TEST_USERNAME,
    }
    assert protected_response.status_code == 200


def test_wrong_password_does_not_create_session(auth_client):
    client, _ = auth_client

    response = client.post(
        "/auth/login",
        json={"username": TEST_USERNAME, "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert SESSION_COOKIE_NAME not in client.cookies
    assert client.get("/api/accounts").status_code == 401


def test_logout_revokes_original_session(auth_client):
    client, _ = auth_client
    assert _login(client).status_code == 200
    assert client.get("/api/accounts").status_code == 200
    original_token = client.cookies[SESSION_COOKIE_NAME]

    logout_response = client.post("/auth/logout")

    assert logout_response.status_code == 200
    assert logout_response.json() == {"authenticated": False}
    assert SESSION_COOKIE_NAME not in client.cookies
    assert client.get("/api/accounts").status_code == 401

    replay_client = TestClient(app_module.app)
    replay_response = replay_client.get(
        "/api/accounts",
        headers={"cookie": f"{SESSION_COOKIE_NAME}={original_token}"},
    )
    replay_client.close()
    assert replay_response.status_code == 401


def test_tampered_cookie_is_rejected(auth_client):
    client, _ = auth_client
    assert _login(client).status_code == 200
    token = client.cookies[SESSION_COOKIE_NAME]
    replacement = "A" if token[0] != "A" else "B"
    tampered = replacement + token[1:]
    client.cookies.clear()

    response = client.get(
        "/api/accounts",
        headers={"cookie": f"{SESSION_COOKIE_NAME}={tampered}"},
    )

    assert response.status_code == 401


def test_websocket_requires_valid_session(auth_client):
    client, _ = auth_client

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 1008

    assert _login(client).status_code == 200
    with client.websocket_connect("/ws") as websocket:
        assert websocket is not None


def test_fixed_secret_keeps_unexpired_session_valid_across_manager_restart(auth_client):
    client, _ = auth_client
    assert _login(client).status_code == 200
    token = client.cookies[SESSION_COOKIE_NAME]

    auth_module.reset_session_manager()
    restarted_client = TestClient(app_module.app)
    response = restarted_client.get(
        "/api/accounts",
        headers={"cookie": f"{SESSION_COOKIE_NAME}={token}"},
    )
    restarted_client.close()

    assert response.status_code == 200


def test_expired_session_is_rejected():
    now = [1_800_000_000]
    manager = SessionManager(
        secret=TEST_SESSION_SECRET,
        username=TEST_USERNAME,
        password=TEST_PASSWORD,
        max_age_seconds=3600,
        cookie_secure=False,
        clock=lambda: now[0],
        store=InMemorySessionStore(),
    )
    token = manager.create_session()

    now[0] += 3600

    assert manager.read_session(token) is None


def test_secure_cookie_attribute_follows_configuration():
    manager = SessionManager(
        secret=TEST_SESSION_SECRET,
        username=TEST_USERNAME,
        password=TEST_PASSWORD,
        max_age_seconds=3600,
        cookie_secure=True,
        store=InMemorySessionStore(),
    )
    response = Response()

    manager.set_cookie(response, manager.create_session())

    assert "Secure" in response.headers["set-cookie"]


@pytest.mark.parametrize("session_secret", [None, "too-short"])
def test_production_rejects_missing_or_weak_session_secret(session_secret):
    config = SimpleNamespace(
        app_env="production",
        session_secret=session_secret,
        web_username=TEST_USERNAME,
        web_password=TEST_PASSWORD,
        session_max_age_seconds=3600,
        session_cookie_secure=True,
    )

    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        SessionManager.from_settings(config)


def test_production_rejects_insecure_session_cookie():
    config = SimpleNamespace(
        app_env="production",
        session_secret=TEST_SESSION_SECRET,
        web_username=TEST_USERNAME,
        web_password=TEST_PASSWORD,
        session_max_age_seconds=3600,
        session_cookie_secure=False,
    )

    with pytest.raises(RuntimeError, match="SESSION_COOKIE_SECURE"):
        SessionManager.from_settings(config)


def test_development_allows_insecure_session_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    config = SimpleNamespace(
        app_env="development",
        session_secret=TEST_SESSION_SECRET,
        web_username=TEST_USERNAME,
        web_password=TEST_PASSWORD,
        session_max_age_seconds=3600,
        session_cookie_secure=False,
    )

    manager = SessionManager.from_settings(config)

    assert manager.cookie_secure is False


def test_development_generates_temporary_secret_and_warns(
    tmp_path,
    monkeypatch,
    caplog,
):
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    config = SimpleNamespace(
        app_env="development",
        session_secret=None,
        web_username=TEST_USERNAME,
        web_password=TEST_PASSWORD,
        session_max_age_seconds=3600,
        session_cookie_secure=False,
    )

    with caplog.at_level(logging.WARNING):
        first_manager = SessionManager.from_settings(config)
        token = first_manager.create_session()
        restarted_manager = SessionManager.from_settings(config)

    assert restarted_manager.read_session(token) is None
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "temporary secret" in messages
    assert "restarts" in messages


def test_default_credentials_emit_warning_without_password(caplog, monkeypatch):
    monkeypatch.setattr(auth_module.app_settings, "app_env", "development")
    monkeypatch.setattr(auth_module.app_settings, "web_username", "admin")
    monkeypatch.setattr(auth_module.app_settings, "web_password", "admin123")
    monkeypatch.setattr(
        auth_module.app_settings,
        "session_secret",
        TEST_SESSION_SECRET,
    )
    auth_module.reset_session_manager()

    with caplog.at_level(logging.CRITICAL):
        auth_module.initialize_session_security()

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "Default Web UI credentials" in messages
    assert "admin123" not in messages
    auth_module.reset_session_manager()
