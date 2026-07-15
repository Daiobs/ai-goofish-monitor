"""Shared browser proxy and account-session preparation for crawler runs."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit


GOOFISH_HOST = "goofish.com"
SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5"}
DEFAULT_PROXY_PORTS = {"http": 80, "https": 443, "socks5": 1080}
SAFE_SNAPSHOT_HEADERS = {"accept", "accept-language"}
_BROWSER_MAJOR_PATTERN = re.compile(r"(?:Chrome|Chromium)/(\d+)", re.IGNORECASE)
_PLAIN_BROWSER_MAJOR_PATTERN = re.compile(r"^\s*(\d+)(?:\.|$)")


class BrowserSessionError(ValueError):
    """Raised when a state file cannot safely restore a Goofish session."""


class BrowserProxyError(ValueError):
    """Raised when the explicit scraper proxy is malformed."""


@dataclass(frozen=True)
class BrowserProxyConfig:
    mode: str
    server: str | None = None
    display: str = "direct"
    host: str | None = None
    port: int | None = None
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        return self.server is not None

    def playwright_options(self) -> dict[str, str] | None:
        if not self.server:
            return None
        options = {"server": self.server}
        if self.username is not None:
            options["username"] = self.username
        if self.password is not None:
            options["password"] = self.password
        return options


@dataclass(frozen=True)
class ProxyProbeResult:
    success: bool
    reason: str


@dataclass(frozen=True)
class BrowserSessionPlan:
    snapshot_kind: str
    target_origin: str
    storage_state: dict[str, Any] = field(repr=False)
    session_storage_by_origin: dict[str, dict[str, str]] = field(repr=False)
    context_options: dict[str, Any] = field(repr=False)
    extra_headers: dict[str, str] = field(repr=False)
    cookie_count: int
    local_storage_count: int
    session_storage_count: int
    snapshot_browser_major: int | None
    snapshot_platform: str | None
    is_mobile: bool


def _is_goofish_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    return host == GOOFISH_HOST or host.endswith(f".{GOOFISH_HOST}")


def _safe_origin(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise BrowserSessionError("增强快照缺少有效的 Goofish 页面 origin") from exc
    if parsed.scheme not in {"http", "https"} or not _is_goofish_host(hostname):
        raise BrowserSessionError("增强快照的页面 origin 不是 Goofish")
    display_host = f"[{hostname}]" if hostname and ":" in hostname else hostname
    netloc = f"{display_host}:{port}" if port is not None else str(display_host)
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _validate_storage_map(value: Any, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BrowserSessionError(f"增强快照的 {label} 结构无效")
    validated: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise BrowserSessionError(f"增强快照的 {label} 条目结构无效")
        validated[key] = item
    return validated


def _filter_goofish_cookies(cookies: Any) -> list[dict[str, Any]]:
    if not isinstance(cookies, list):
        raise BrowserSessionError("登录状态的 Cookie 结构无效")
    filtered: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise BrowserSessionError("登录状态包含无效 Cookie 条目")
        domain = str(cookie.get("domain") or "").lstrip(".")
        if _is_goofish_host(domain):
            filtered.append(cookie)
    if not filtered:
        raise BrowserSessionError("登录状态中没有可恢复的 Goofish Cookie")
    return filtered


def _extract_browser_major(user_agent: str | None) -> int | None:
    if not user_agent:
        return None
    match = _BROWSER_MAJOR_PATTERN.search(user_agent)
    return int(match.group(1)) if match else None


def browser_major(version: str | None) -> int | None:
    from_user_agent = _extract_browser_major(version)
    if from_user_agent is not None:
        return from_user_agent
    match = _PLAIN_BROWSER_MAJOR_PATTERN.search(version or "")
    return int(match.group(1)) if match else None


def _looks_mobile(user_agent: str | None) -> bool:
    lowered = (user_agent or "").lower()
    return any(marker in lowered for marker in ("mobile", "android", "iphone"))


def _default_context_options(*, mobile: bool) -> dict[str, Any]:
    if mobile:
        return {
            "user_agent": (
                "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
                "Mobile Safari/537.36"
            ),
            "viewport": {"width": 412, "height": 915},
            "device_scale_factor": 2.625,
            "is_mobile": True,
            "has_touch": True,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "color_scheme": "light",
        }
    return {
        "viewport": {"width": 1365, "height": 768},
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "color_scheme": "light",
    }


def _enhanced_context_options(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], bool, int | None, str | None]:
    env = snapshot.get("env") if isinstance(snapshot.get("env"), dict) else {}
    headers = snapshot.get("headers") if isinstance(snapshot.get("headers"), dict) else {}
    navigator = env.get("navigator") if isinstance(env.get("navigator"), dict) else {}
    screen = env.get("screen") if isinstance(env.get("screen"), dict) else {}
    intl = env.get("intl") if isinstance(env.get("intl"), dict) else {}

    user_agent = (
        headers.get("User-Agent")
        or headers.get("user-agent")
        or navigator.get("userAgent")
    )
    if user_agent is not None and not isinstance(user_agent, str):
        raise BrowserSessionError("增强快照的 User-Agent 结构无效")
    is_mobile = _looks_mobile(user_agent)
    options = _default_context_options(mobile=is_mobile)
    if user_agent:
        options["user_agent"] = user_agent

    language = navigator.get("language")
    accept_language = headers.get("Accept-Language") or headers.get("accept-language")
    if isinstance(accept_language, str) and accept_language.strip():
        options["locale"] = accept_language.split(",", 1)[0].strip()
    elif isinstance(language, str) and language.strip():
        options["locale"] = language.strip()

    timezone = intl.get("timeZone")
    if isinstance(timezone, str) and timezone.strip():
        options["timezone_id"] = timezone.strip()

    width = screen.get("width")
    height = screen.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        if width > 0 and height > 0:
            options["viewport"] = {"width": int(width), "height": int(height)}
    dpr = screen.get("devicePixelRatio")
    if isinstance(dpr, (int, float)) and dpr > 0:
        options["device_scale_factor"] = float(dpr)
    touch_points = navigator.get("maxTouchPoints")
    if isinstance(touch_points, (int, float)):
        options["has_touch"] = touch_points > 0

    platform = navigator.get("platform")
    if not isinstance(platform, str):
        platform = headers.get("sec-ch-ua-platform")
    if isinstance(platform, str):
        platform = platform.strip('"') or None
    else:
        platform = None
    return options, is_mobile, _extract_browser_major(user_agent), platform


def _safe_extra_headers(raw_headers: Any) -> dict[str, str]:
    if raw_headers is None:
        return {}
    if not isinstance(raw_headers, dict):
        raise BrowserSessionError("增强快照的 Header 结构无效")
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.lower() in SAFE_SNAPSHOT_HEADERS:
            headers[key] = value
    return headers


def _load_standard_snapshot(snapshot: Mapping[str, Any]) -> BrowserSessionPlan:
    cookies = _filter_goofish_cookies(snapshot.get("cookies"))
    raw_origins = snapshot.get("origins", [])
    if not isinstance(raw_origins, list):
        raise BrowserSessionError("标准登录状态的 origins 结构无效")
    origins: list[dict[str, Any]] = []
    local_count = 0
    for origin_entry in raw_origins:
        if not isinstance(origin_entry, dict):
            raise BrowserSessionError("标准登录状态包含无效 origin 条目")
        raw_origin = origin_entry.get("origin")
        if not isinstance(raw_origin, str):
            raise BrowserSessionError("标准登录状态包含无效 origin")
        try:
            origin = _safe_origin(raw_origin)
        except BrowserSessionError:
            continue
        local_storage = origin_entry.get("localStorage", [])
        if not isinstance(local_storage, list):
            raise BrowserSessionError("标准登录状态的 localStorage 结构无效")
        validated_local_storage: list[dict[str, str]] = []
        for item in local_storage:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("value"), str)
            ):
                raise BrowserSessionError("标准登录状态包含无效 localStorage 条目")
            validated_local_storage.append(
                {"name": item["name"], "value": item["value"]}
            )
        local_count += len(validated_local_storage)
        origins.append(
            {"origin": origin, "localStorage": validated_local_storage}
        )
    target_origin = origins[0]["origin"] if origins else "https://www.goofish.com"
    return BrowserSessionPlan(
        snapshot_kind="standard",
        target_origin=target_origin,
        storage_state={"cookies": cookies, "origins": origins},
        session_storage_by_origin={},
        context_options=_default_context_options(mobile=True),
        extra_headers={},
        cookie_count=len(cookies),
        local_storage_count=local_count,
        session_storage_count=0,
        snapshot_browser_major=None,
        snapshot_platform=None,
        is_mobile=True,
    )


def _load_enhanced_snapshot(snapshot: Mapping[str, Any]) -> BrowserSessionPlan:
    raw_page_url = snapshot.get("pageUrl")
    if not isinstance(raw_page_url, str):
        page = snapshot.get("page")
        raw_page_url = page.get("pageUrl") if isinstance(page, dict) else None
    if not isinstance(raw_page_url, str):
        raise BrowserSessionError("增强快照缺少页面 URL")
    target_origin = _safe_origin(raw_page_url)
    cookies = _filter_goofish_cookies(snapshot.get("cookies"))

    storage = snapshot.get("storage")
    if not isinstance(storage, dict):
        raise BrowserSessionError("增强快照缺少有效的 storage")
    local_storage = _validate_storage_map(storage.get("local"), "localStorage")
    session_storage = _validate_storage_map(storage.get("session"), "sessionStorage")
    context_options, is_mobile, snapshot_major, platform = _enhanced_context_options(snapshot)
    extra_headers = _safe_extra_headers(snapshot.get("headers"))
    origin_entry = {
        "origin": target_origin,
        "localStorage": [
            {"name": key, "value": value}
            for key, value in sorted(local_storage.items())
        ],
    }
    return BrowserSessionPlan(
        snapshot_kind="enhanced",
        target_origin=target_origin,
        storage_state={"cookies": cookies, "origins": [origin_entry]},
        session_storage_by_origin={target_origin: session_storage} if session_storage else {},
        context_options=context_options,
        extra_headers=extra_headers,
        cookie_count=len(cookies),
        local_storage_count=len(local_storage),
        session_storage_count=len(session_storage),
        snapshot_browser_major=snapshot_major,
        snapshot_platform=platform,
        is_mobile=is_mobile,
    )


def load_browser_session(state_file: str | Path) -> BrowserSessionPlan:
    path = Path(state_file)
    if not path.is_file():
        raise BrowserSessionError("登录状态文件不存在或不可读取")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BrowserSessionError("登录状态文件不是有效 JSON") from exc
    if not isinstance(snapshot, dict):
        raise BrowserSessionError("登录状态文件顶层必须是 object")
    enhanced = any(key in snapshot for key in ("env", "headers", "page", "storage"))
    return _load_enhanced_snapshot(snapshot) if enhanced else _load_standard_snapshot(snapshot)


def build_session_storage_script(plan: BrowserSessionPlan) -> str | None:
    if not plan.session_storage_by_origin:
        return None
    payload = json.dumps(
        plan.session_storage_by_origin,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"""
        (() => {{
            const valuesByOrigin = {payload};
            const values = valuesByOrigin[window.location.origin];
            if (!values) return;
            for (const [key, value] of Object.entries(values)) {{
                window.sessionStorage.setItem(key, value);
            }}
        }})();
    """


def browser_init_script() -> str:
    return """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        window.chrome = window.chrome || {runtime: {}};
        const originalQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : originalQuery(parameters)
        );
    """


def resolve_browser_proxy(
    raw_url: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> BrowserProxyConfig:
    if raw_url is None:
        env = environ if environ is not None else os.environ
        raw_url = env.get("SCRAPER_PROXY_URL")
    value = (raw_url or "").strip()
    if not value:
        return BrowserProxyConfig(mode="direct")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BrowserProxyError("SCRAPER_PROXY_URL 端口无效") from exc
    scheme = parsed.scheme.lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname:
        raise BrowserProxyError("SCRAPER_PROXY_URL 必须是 http、https 或 socks5 URL")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise BrowserProxyError("SCRAPER_PROXY_URL 不应包含路径、query 或 fragment")
    if scheme == "socks5" and (
        parsed.username is not None or parsed.password is not None
    ):
        raise BrowserProxyError(
            "Playwright 不支持带账号密码的 socks5 代理，请改用 HTTP(S) 认证代理"
        )
    port = port or DEFAULT_PROXY_PORTS[scheme]
    host = parsed.hostname
    display_host = f"[{host}]" if ":" in host else host
    netloc = f"{display_host}:{port}"
    server = urlunsplit((scheme, netloc, "", "", ""))
    return BrowserProxyConfig(
        mode="explicit_proxy",
        server=server,
        display=server,
        host=host,
        port=port,
        username=unquote(parsed.username) if parsed.username is not None else None,
        password=unquote(parsed.password) if parsed.password is not None else None,
    )


async def probe_proxy_endpoint(
    proxy: BrowserProxyConfig,
    *,
    timeout_seconds: float = 3.0,
) -> ProxyProbeResult:
    if not proxy.configured:
        return ProxyProbeResult(True, "浏览器使用直连模式")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.host, proxy.port),
            timeout=timeout_seconds,
        )
        del reader
        writer.close()
        await writer.wait_closed()
    except Exception as exc:
        return ProxyProbeResult(False, f"代理端点不可连接 ({type(exc).__name__})")
    return ProxyProbeResult(True, "代理端点 TCP 连接成功")
