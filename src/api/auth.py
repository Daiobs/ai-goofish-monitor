"""Signed session authentication shared by HTTP and WebSocket routes."""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.responses import Response

from src.infrastructure.config.settings import AppSettings, settings as app_settings
from src.infrastructure.persistence.sqlite_connection import init_schema, sqlite_connection


logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "ai_goofish_session"
SESSION_COOKIE_PATH = "/"
MIN_SESSION_SECRET_BYTES = 32
_PRODUCTION_ENVIRONMENTS = {"prod", "production"}
_SESSION_VERSION = 1
_session_manager: Optional["SessionManager"] = None


def _constant_time_equal(left: str, right: str) -> bool:
    """Compare arbitrary Unicode strings without data-dependent early returns."""
    left_digest = hashlib.sha256(left.encode("utf-8")).digest()
    right_digest = hashlib.sha256(right.encode("utf-8")).digest()
    return hmac.compare_digest(left_digest, right_digest)


def _is_strong_session_secret(secret: str) -> bool:
    encoded = secret.encode("utf-8")
    return len(encoded) >= MIN_SESSION_SECRET_BYTES and len(set(encoded)) >= 8


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


@dataclass(frozen=True)
class AuthenticatedSession:
    username: str
    session_id: str
    expires_at: int


class SessionStore(Protocol):
    def create(
        self,
        session_id: str,
        credential_fingerprint: str,
        created_at: int,
        expires_at: int,
    ) -> None: ...

    def is_active(
        self,
        session_id: str,
        credential_fingerprint: str,
        now: int,
    ) -> bool: ...

    def revoke(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """Small test-friendly store with the same semantics as the SQLite store."""

    def __init__(self) -> None:
        self._sessions: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def create(
        self,
        session_id: str,
        credential_fingerprint: str,
        created_at: int,
        expires_at: int,
    ) -> None:
        del created_at
        with self._lock:
            self._sessions[session_id] = (credential_fingerprint, expires_at)

    def is_active(
        self,
        session_id: str,
        credential_fingerprint: str,
        now: int,
    ) -> bool:
        with self._lock:
            self._remove_expired(now)
            stored = self._sessions.get(session_id)
            return stored is not None and hmac.compare_digest(
                stored[0],
                credential_fingerprint,
            )

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _remove_expired(self, now: int) -> None:
        expired = [
            session_id
            for session_id, (_, expires_at) in self._sessions.items()
            if expires_at <= now
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)


class SqliteSessionStore:
    """Persistent server-side session registry used by the application."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path
        self._initialized = False
        self._init_lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with sqlite_connection(self._db_path) as conn:
                init_schema(conn)
            self._initialized = True

    def create(
        self,
        session_id: str,
        credential_fingerprint: str,
        created_at: int,
        expires_at: int,
    ) -> None:
        self._ensure_schema()
        with sqlite_connection(self._db_path) as conn:
            conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (created_at,))
            conn.execute(
                """
                INSERT INTO auth_sessions (
                    session_id, credential_fingerprint, created_at, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (session_id, credential_fingerprint, created_at, expires_at),
            )
            conn.commit()

    def is_active(
        self,
        session_id: str,
        credential_fingerprint: str,
        now: int,
    ) -> bool:
        self._ensure_schema()
        with sqlite_connection(self._db_path, read_only=True) as conn:
            row = conn.execute(
                """
                SELECT credential_fingerprint
                FROM auth_sessions
                WHERE session_id = ? AND expires_at > ?
                """,
                (session_id, now),
            ).fetchone()
        return row is not None and hmac.compare_digest(
            row["credential_fingerprint"],
            credential_fingerprint,
        )

    def revoke(self, session_id: str) -> None:
        self._ensure_schema()
        with sqlite_connection(self._db_path) as conn:
            conn.execute("DELETE FROM auth_sessions WHERE session_id = ?", (session_id,))
            conn.commit()


class SessionManager:
    """Issue and verify authenticated, expiring session cookies."""

    def __init__(
        self,
        *,
        secret: str,
        username: str,
        password: str,
        max_age_seconds: int,
        cookie_secure: bool,
        clock: Callable[[], float] = time.time,
        store: Optional[SessionStore] = None,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._username = username
        self._password = password
        self.max_age_seconds = max_age_seconds
        self.cookie_secure = cookie_secure
        self._clock = clock
        self._store = store or InMemorySessionStore()

    @classmethod
    def from_settings(cls, config: AppSettings) -> "SessionManager":
        configured_secret = (config.session_secret or "").strip()
        is_production = config.app_env.strip().lower() in _PRODUCTION_ENVIRONMENTS

        if not _is_strong_session_secret(configured_secret):
            if is_production:
                raise RuntimeError(
                    "SESSION_SECRET must be set to a strong value in production"
                )
            configured_secret = secrets.token_urlsafe(48)
            logger.warning(
                "SECURITY WARNING: SESSION_SECRET is missing or too weak. "
                "A temporary secret was generated; all sessions will become invalid "
                "when this process restarts."
            )

        if is_production and not config.session_cookie_secure:
            raise RuntimeError(
                "SESSION_COOKIE_SECURE must be true in production"
            )

        return cls(
            secret=configured_secret,
            username=config.web_username,
            password=config.web_password,
            max_age_seconds=config.session_max_age_seconds,
            cookie_secure=config.session_cookie_secure,
            store=SqliteSessionStore(),
        )

    def credentials_are_valid(self, username: str, password: str) -> bool:
        username_matches = _constant_time_equal(username, self._username)
        password_matches = _constant_time_equal(password, self._password)
        return username_matches and password_matches

    def _credential_fingerprint(self) -> str:
        payload = (
            b"credentials\0"
            + self._username.encode("utf-8")
            + b"\0"
            + self._password.encode("utf-8")
        )
        digest = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return _encode_base64(digest)

    def create_session(self) -> str:
        issued_at = int(self._clock())
        credential_fingerprint = self._credential_fingerprint()
        payload = {
            "v": _SESSION_VERSION,
            "sub": self._username,
            "sid": secrets.token_urlsafe(24),
            "iat": issued_at,
            "exp": issued_at + self.max_age_seconds,
            "cred": credential_fingerprint,
        }
        encoded_payload = _encode_base64(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = hmac.new(
            self._secret,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        self._store.create(
            payload["sid"],
            credential_fingerprint,
            issued_at,
            payload["exp"],
        )
        return f"{encoded_payload}.{_encode_base64(signature)}"

    def read_session(self, token: Optional[str]) -> Optional[AuthenticatedSession]:
        if not token:
            return None

        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            supplied_signature = _decode_base64(encoded_signature)
            expected_signature = hmac.new(
                self._secret,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None

            payload = json.loads(_decode_base64(encoded_payload).decode("utf-8"))
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None

        required_strings = ("sub", "sid", "cred")
        if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), str) for key in required_strings
        ):
            return None
        if payload.get("v") != _SESSION_VERSION:
            return None

        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        if (
            not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
        ):
            return None

        now = int(self._clock())
        if (
            issued_at > now + 60
            or expires_at <= now
            or expires_at <= issued_at
            or expires_at - issued_at > self.max_age_seconds
        ):
            return None
        if not _constant_time_equal(payload["sub"], self._username):
            return None
        credential_fingerprint = self._credential_fingerprint()
        if not hmac.compare_digest(payload["cred"], credential_fingerprint):
            return None
        try:
            if not self._store.is_active(
                payload["sid"],
                credential_fingerprint,
                now,
            ):
                return None
        except Exception:
            logger.error("Session store validation failed")
            return None

        return AuthenticatedSession(
            username=self._username,
            session_id=payload["sid"],
            expires_at=expires_at,
        )

    def revoke_session(self, token: Optional[str]) -> None:
        session = self.read_session(token)
        if session is not None:
            self._store.revoke(session.session_id)

    def set_cookie(self, response: Response, token: str) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.max_age_seconds)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=self.max_age_seconds,
            expires=expires_at,
            path=SESSION_COOKIE_PATH,
            secure=self.cookie_secure,
            httponly=True,
            samesite="strict",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            path=SESSION_COOKIE_PATH,
            secure=self.cookie_secure,
            httponly=True,
            samesite="strict",
        )


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager.from_settings(app_settings)
    return _session_manager


def reset_session_manager() -> None:
    """Reset cached security state after configuration changes or in tests."""
    global _session_manager
    _session_manager = None


def initialize_session_security() -> SessionManager:
    manager = get_session_manager()
    if _constant_time_equal(app_settings.web_username, "admin") and _constant_time_equal(
        app_settings.web_password,
        "admin123",
    ):
        logger.critical(
            "SECURITY WARNING: Default Web UI credentials are active. "
            "Change both administrator credentials before exposing this service."
        )
    return manager


def require_authenticated_session(
    request: Request,
    manager: SessionManager = Depends(get_session_manager),
) -> AuthenticatedSession:
    session = manager.read_session(request.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return session


def read_websocket_session(websocket: WebSocket) -> Optional[AuthenticatedSession]:
    return get_session_manager().read_session(
        websocket.cookies.get(SESSION_COOKIE_NAME)
    )
