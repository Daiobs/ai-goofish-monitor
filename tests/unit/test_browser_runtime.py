import json
from pathlib import Path

import pytest

from src.services.browser_runtime import BrowserProxyError
from src.services.browser_runtime import BrowserSessionError
from src.services.browser_runtime import browser_major
from src.services.browser_runtime import build_session_storage_script
from src.services.browser_runtime import load_browser_session
from src.services.browser_runtime import resolve_browser_proxy


def _write_snapshot(tmp_path: Path, payload: object) -> Path:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    return state_file


def _goofish_cookie(name: str = "unb", value: str = "account-token") -> dict:
    return {
        "name": name,
        "value": value,
        "domain": ".goofish.com",
        "path": "/",
    }


def test_explicit_proxy_parses_credentials_without_exposing_them() -> None:
    expected_username = "monitor@team"
    expected_value = "p@ss:word"
    proxy = resolve_browser_proxy(
        "http://monitor%40team:p%40ss%3Aword@proxy.internal"  # pragma: allowlist secret
    )

    assert proxy.mode == "explicit_proxy"
    assert proxy.configured is True
    assert proxy.server == "http://proxy.internal:80"
    assert proxy.display == "http://proxy.internal:80"
    assert proxy.host == "proxy.internal"
    assert proxy.port == 80
    assert proxy.username == expected_username
    assert proxy.password == expected_value
    assert proxy.playwright_options() == {
        "server": "http://proxy.internal:80",
        "username": expected_username,
        "password": expected_value,
    }

    safe_text = f"{proxy!r} {proxy.display}"
    assert "monitor" not in safe_text
    assert expected_value not in safe_text


def test_proxy_validation_error_does_not_echo_credentials() -> None:
    raw_url = "http://proxy-user:super-secret@proxy.internal/private"  # pragma: allowlist secret

    with pytest.raises(BrowserProxyError) as exc_info:
        resolve_browser_proxy(raw_url)

    message = str(exc_info.value)
    assert "proxy-user" not in message
    assert "super-secret" not in message


def test_authenticated_socks5_is_rejected_without_echoing_credentials() -> None:
    raw_url = "socks5://proxy-user:super-secret@proxy.internal:1080"  # pragma: allowlist secret

    with pytest.raises(BrowserProxyError) as exc_info:
        resolve_browser_proxy(raw_url)

    message = str(exc_info.value)
    assert "socks5" in message
    assert "proxy-user" not in message
    assert "super-secret" not in message


def test_browser_major_accepts_playwright_version_format() -> None:
    assert browser_major("149.0.7827.0") == 149
    assert browser_major("Chromium/149.0.0.0") == 149


def test_missing_explicit_proxy_uses_direct_mode() -> None:
    proxy = resolve_browser_proxy(
        environ={"HTTP_PROXY": "http://ambient-proxy.invalid:3128"}
    )

    assert proxy.mode == "direct"
    assert proxy.configured is False
    assert proxy.server is None
    assert proxy.display == "direct"
    assert proxy.playwright_options() is None


def test_docker_host_proxy_is_forwarded_to_playwright() -> None:
    proxy = resolve_browser_proxy("http://host.docker.internal:7897")

    assert proxy.display == "http://host.docker.internal:7897"
    assert proxy.playwright_options() == {
        "server": "http://host.docker.internal:7897"
    }


def test_standard_snapshot_restores_only_goofish_cookies_and_local_storage(
    tmp_path: Path,
) -> None:
    goofish_cookie = _goofish_cookie()
    snapshot = {
        "cookies": [
            goofish_cookie,
            {
                "name": "external-session",
                "value": "must-not-be-restored",
                "domain": ".example.com",
                "path": "/",
            },
        ],
        "origins": [
            {
                "origin": "https://www.goofish.com",
                "localStorage": [
                    {"name": "account", "value": "123"},
                    {"name": "preferences", "value": "compact"},
                ],
            },
            {
                "origin": "https://example.com",
                "localStorage": [
                    {"name": "external", "value": "must-not-be-restored"}
                ],
            },
        ],
    }

    plan = load_browser_session(_write_snapshot(tmp_path, snapshot))

    assert plan.snapshot_kind == "standard"
    assert plan.target_origin == "https://www.goofish.com"
    assert plan.storage_state == {
        "cookies": [goofish_cookie],
        "origins": [snapshot["origins"][0]],
    }
    assert plan.session_storage_by_origin == {}
    assert plan.cookie_count == 1
    assert plan.local_storage_count == 2
    assert plan.session_storage_count == 0
    assert build_session_storage_script(plan) is None


def test_enhanced_snapshot_restores_all_storage_with_desktop_context(
    tmp_path: Path,
) -> None:
    desktop_user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    excluded_header_value = "must-not-be-forwarded"
    goofish_cookie = _goofish_cookie("cookie2", "goofish-session")
    snapshot = {
        "pageUrl": "https://www.goofish.com/search?q=camera#results",
        "cookies": [
            goofish_cookie,
            {
                "name": "external",
                "value": "ignored",
                "domain": ".example.com",
                "path": "/",
            },
        ],
        "storage": {
            "local": {"z-last": "z-value", "a-first": "a-value"},
            "session": {"sid": "session-token", "view": "grid"},
        },
        "headers": {
            "User-Agent": desktop_user_agent,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": excluded_header_value,
            "Authorization": excluded_header_value,
        },
        "env": {
            "navigator": {
                "userAgent": "ignored in favor of the captured header",
                "platform": "MacIntel",
                "language": "en-US",
                "maxTouchPoints": 0,
            },
            "screen": {
                "width": 1920,
                "height": 1080,
                "devicePixelRatio": 2,
            },
            "intl": {"timeZone": "Asia/Shanghai"},
        },
    }

    plan = load_browser_session(_write_snapshot(tmp_path, snapshot))

    assert plan.snapshot_kind == "enhanced"
    assert plan.target_origin == "https://www.goofish.com"
    assert plan.storage_state == {
        "cookies": [goofish_cookie],
        "origins": [
            {
                "origin": "https://www.goofish.com",
                "localStorage": [
                    {"name": "a-first", "value": "a-value"},
                    {"name": "z-last", "value": "z-value"},
                ],
            }
        ],
    }
    assert plan.session_storage_by_origin == {
        "https://www.goofish.com": {
            "sid": "session-token",
            "view": "grid",
        }
    }
    assert (plan.cookie_count, plan.local_storage_count, plan.session_storage_count) == (
        1,
        2,
        2,
    )
    assert plan.extra_headers == {
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    assert plan.is_mobile is False
    assert plan.context_options == {
        "user_agent": desktop_user_agent,
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 2.0,
        "is_mobile": False,
        "has_touch": False,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "color_scheme": "light",
    }
    assert plan.snapshot_browser_major == 131
    assert plan.snapshot_platform == "MacIntel"

    session_script = build_session_storage_script(plan)
    assert session_script is not None
    assert '"https://www.goofish.com"' in session_script
    assert '"sid":"session-token"' in session_script
    assert '"view":"grid"' in session_script
    assert "window.sessionStorage.setItem(key, value)" in session_script


def test_enhanced_snapshot_rejects_non_goofish_origin(tmp_path: Path) -> None:
    snapshot = {
        "pageUrl": "https://accounts.example.com/session/export",
        "cookies": [_goofish_cookie()],
        "storage": {"local": {}, "session": {}},
    }

    with pytest.raises(BrowserSessionError, match="origin.*Goofish"):
        load_browser_session(_write_snapshot(tmp_path, snapshot))


@pytest.mark.parametrize(
    ("raw_contents", "message"),
    [
        ("not-json", "有效 JSON"),
        (json.dumps(["not", "an", "object"]), "顶层必须是 object"),
        (json.dumps({"cookies": "not-a-list", "origins": []}), "Cookie 结构无效"),
        (
            json.dumps(
                {
                    "cookies": [_goofish_cookie()],
                    "origins": "not-a-list",
                }
            ),
            "origins 结构无效",
        ),
        (
            json.dumps(
                {
                    "pageUrl": "https://www.goofish.com/search",
                    "cookies": [_goofish_cookie()],
                    "storage": {"local": [], "session": {}},
                }
            ),
            "localStorage 结构无效",
        ),
        (
            json.dumps(
                {
                    "cookies": [_goofish_cookie()],
                    "origins": [
                        {
                            "origin": "https://www.goofish.com",
                            "localStorage": [{"name": "missing-value"}],
                        }
                    ],
                }
            ),
            "无效 localStorage 条目",
        ),
    ],
)
def test_malformed_snapshot_is_rejected(
    tmp_path: Path,
    raw_contents: str,
    message: str,
) -> None:
    state_file = tmp_path / "malformed-state.json"
    state_file.write_text(raw_contents, encoding="utf-8")

    with pytest.raises(BrowserSessionError, match=message):
        load_browser_session(state_file)
