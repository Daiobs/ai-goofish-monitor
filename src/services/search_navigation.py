"""Focused Goofish search navigation and failure classification."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.services.search_pagination import is_search_results_response


LOGIN_TEXT_MARKERS = ("请登录", "扫码登录", "短信登录", "账号登录")
RISK_TEXT_MARKERS = ("安全验证", "请完成验证", "拖动滑块", "验证码")
ERROR_TEXT_MARKERS = ("页面走丢了", "访问出错", "网络开小差", "系统繁忙")
API_RISK_MARKERS = ("FAIL_SYS_USER_VALIDATE",)
API_LOGIN_MARKERS = (
    "FAIL_SYS_SESSION_EXPIRED",
    "FAIL_SYS_TOKEN_EXPIRED",
    "FAIL_SYS_USER_LOGIN",
)
RISK_SELECTORS = ("div.baxia-dialog-mask", "div.J_MIDDLEWARE_FRAME_WIDGET")
RESULT_SELECTORS = (
    "[class*='feeds-item-wrap']",
    "[class*='search-item']",
    "a[href*='/item']",
)


@dataclass(frozen=True)
class PageClassification:
    failure_kind: str | None = None
    reason: str = ""
    suggestion: str = ""
    has_result_dom: bool = False


@dataclass(frozen=True)
class CapturedSearchResponse:
    raw_response: Any = field(repr=False)
    payload: dict[str, Any] = field(repr=False)
    source: str

    @property
    def ok(self) -> bool:
        return bool(getattr(self.raw_response, "ok", True))

    async def json(self) -> dict[str, Any]:
        return self.payload


@dataclass(frozen=True)
class SearchResponseDiagnostic:
    http_status: int | None
    content_type: str
    api_path: str
    is_search_response: bool
    json_parsed: bool
    json_type: str
    ret_codes: tuple[str, ...]
    top_level_fields: tuple[tuple[str, str], ...]
    data_fields: tuple[tuple[str, str], ...]
    has_result_list: bool
    result_count: int
    first_result_is_object: bool
    risk_marker: bool
    login_marker: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "http_status": self.http_status,
            "content_type": self.content_type,
            "api_path": self.api_path,
            "is_search_response": self.is_search_response,
            "json_parsed": self.json_parsed,
            "json_type": self.json_type,
            "ret_codes": list(self.ret_codes),
            "top_level_fields": [
                {"name": name, "type": value_type}
                for name, value_type in self.top_level_fields
            ],
            "data_fields": [
                {"name": name, "type": value_type}
                for name, value_type in self.data_fields
            ],
            "has_result_list": self.has_result_list,
            "result_count": self.result_count,
            "first_result_is_object": self.first_result_is_object,
            "risk_marker": self.risk_marker,
            "login_marker": self.login_marker,
        }


@dataclass(frozen=True)
class SearchNavigationResult:
    success: bool
    failure_kind: str
    reason: str
    suggestion: str
    current_url: str
    page_title: str
    source: str | None = None
    response: CapturedSearchResponse | None = field(default=None, repr=False)
    observed_requests: tuple[str, ...] = ()
    diagnostics: tuple[SearchResponseDiagnostic, ...] = ()
    result_count: int = 0


def _is_goofish_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    return host == "goofish.com" or host.endswith(".goofish.com")


def safe_goofish_url(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return "invalid-url"
    if not _is_goofish_host(hostname):
        return "external-url"
    display_host = f"[{hostname}]" if hostname and ":" in hostname else hostname
    netloc = f"{display_host}:{port}" if port is not None else str(display_host)
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _is_login_url(raw_url: str) -> bool:
    lowered = (raw_url or "").lower()
    return "passport.goofish.com" in lowered or "mini_login" in lowered


def _resource_type(response: Any) -> str:
    request = getattr(response, "request", None)
    return str(getattr(request, "resource_type", "") or "").lower()


def is_goofish_data_response(response: Any) -> bool:
    try:
        parsed = urlsplit(str(getattr(response, "url", "") or ""))
    except ValueError:
        return False
    if not _is_goofish_host(parsed.hostname):
        return False
    return _resource_type(response) in {"xhr", "fetch"} or "search" in parsed.path.lower()


def _find_result_list(value: Any, *, depth: int = 0) -> list[Any] | None:
    if depth > 5:
        return None
    if isinstance(value, dict):
        result_list = value.get("resultList")
        if isinstance(result_list, list):
            return result_list
        for child in value.values():
            found = _find_result_list(child, depth=depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value[:10]:
            found = _find_result_list(child, depth=depth + 1)
            if found is not None:
                return found
    return None


def normalize_search_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    result_list = _find_result_list(payload)
    if result_list is None:
        return None
    data = payload.get("data")
    if isinstance(data, dict) and data.get("resultList") is result_list:
        return payload
    return {"data": {"resultList": result_list}}


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__[:40]


def _safe_field_name(value: Any) -> str:
    cleaned = "".join(
        character
        for character in str(value or "")
        if character.isascii()
        and (character.isalnum() or character in {"_", "-", "."})
    )
    return cleaned[:80] or "UNKNOWN"


def _field_types(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(
        (_safe_field_name(key), _value_type(child))
        for key, child in list(value.items())[:30]
    )


def _ret_codes(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    ret = payload.get("ret")
    values = [ret] if isinstance(ret, str) else ret
    if not isinstance(values, list):
        return ()
    codes: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        code = _safe_field_name(value.split("::", 1)[0].strip().upper())
        if code != "UNKNOWN":
            codes.append(code)
    return tuple(codes[:10])


def _is_success_ret_code(code: str) -> bool:
    return code == "SUCCESS" or code.startswith("SUCCESS_")


def _has_success_ret(payload: Any) -> bool:
    codes = _ret_codes(payload)
    return bool(codes) and all(_is_success_ret_code(code) for code in codes)


def _result_list(payload: dict[str, Any] | None) -> list[Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    result_list = data.get("resultList") if isinstance(data, dict) else None
    return result_list if isinstance(result_list, list) else None


def _is_parseable_search_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    data = item.get("data")
    item_data = data.get("item") if isinstance(data, dict) else None
    main = item_data.get("main") if isinstance(item_data, dict) else None
    ex_content = main.get("exContent") if isinstance(main, dict) else None
    target_url = main.get("targetUrl") if isinstance(main, dict) else None
    if not isinstance(ex_content, dict) or not isinstance(target_url, str):
        return False
    return bool(target_url.strip()) and any(
        isinstance(ex_content.get(key), (str, int))
        and str(ex_content.get(key)).strip()
        for key in ("itemId", "title")
    )


def _result_list_is_parseable(result_list: list[Any]) -> bool:
    if not result_list:
        return True
    return all(isinstance(item, dict) for item in result_list) and any(
        _is_parseable_search_item(item) for item in result_list
    )


def _safe_content_type(response: Any) -> str:
    headers = getattr(response, "headers", {}) or {}
    if not isinstance(headers, dict):
        return ""
    value = str(headers.get("content-type", "") or "")
    return value.split(";", 1)[0].strip().lower()[:80]


def _safe_api_path(response: Any) -> str:
    try:
        parsed = urlsplit(str(getattr(response, "url", "") or ""))
    except ValueError:
        return "invalid-url"
    if not _is_goofish_host(parsed.hostname):
        return "external-url"
    return parsed.path[:300] or "/"


def build_search_response_diagnostic(
    response: Any,
    payload: Any,
    *,
    is_search_response: bool,
    marker_classification: PageClassification | None = None,
    json_parsed: bool = True,
) -> SearchResponseDiagnostic:
    normalized = normalize_search_payload(payload)
    result_list = _result_list(normalized)
    status = getattr(response, "status", None)
    return SearchResponseDiagnostic(
        http_status=status if isinstance(status, int) else None,
        content_type=_safe_content_type(response),
        api_path=_safe_api_path(response),
        is_search_response=is_search_response,
        json_parsed=json_parsed,
        json_type=_value_type(payload),
        ret_codes=_ret_codes(payload),
        top_level_fields=_field_types(payload),
        data_fields=_field_types(payload.get("data"))
        if isinstance(payload, dict)
        else (),
        has_result_list=result_list is not None,
        result_count=len(result_list) if result_list is not None else 0,
        first_result_is_object=bool(
            result_list and isinstance(result_list[0], dict)
        ),
        risk_marker=bool(
            marker_classification
            and marker_classification.failure_kind == "risk_control"
        ),
        login_marker=bool(
            marker_classification
            and marker_classification.failure_kind == "login_required"
        ),
    )


def _payload_strings(value: Any, *, depth: int = 0):
    if depth > 5:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for child in value.values():
            yield from _payload_strings(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value[:50]:
            yield from _payload_strings(child, depth=depth + 1)


def classify_search_payload(payload: Any) -> PageClassification | None:
    for value in _payload_strings(payload):
        upper = value.upper()
        marker = next((item for item in API_RISK_MARKERS if item in upper), None)
        if marker:
            return PageClassification(
                failure_kind="risk_control",
                reason=f"搜索数据响应触发闲鱼风险控制 ({marker})",
                suggestion="停止任务并在浏览器中完成人工验证，之后重新导出账号状态",
            )
        marker = next((item for item in API_LOGIN_MARKERS if item in upper), None)
        if marker:
            return PageClassification(
                failure_kind="login_required",
                reason=f"搜索数据响应显示登录状态失效 ({marker})",
                suggestion="重新导出已登录账号状态后再运行预检",
            )
    return None


def classify_search_response_status(
    response: Any,
    payload: Any,
) -> PageClassification | None:
    status = getattr(response, "status", None)
    if not bool(getattr(response, "ok", True)):
        status_label = status if isinstance(status, int) else "unknown"
        return PageClassification(
            failure_kind="search_page_failed",
            reason=f"闲鱼搜索接口返回 HTTP {status_label}",
            suggestion="确认代理出口和闲鱼服务状态后再运行预检",
        )
    if not isinstance(payload, dict):
        return None
    ret_codes = _ret_codes(payload)
    if ret_codes and not _has_success_ret(payload):
        failure_code = next(
            (
                code
                for code in ret_codes
                if not _is_success_ret_code(code)
            ),
            ret_codes[0],
        )
        return PageClassification(
            failure_kind="search_page_failed",
            reason=f"闲鱼搜索接口返回失败状态 ({failure_code})",
            suggestion="检查登录状态、验证页面和当前搜索接口",
        )
    return None


def describe_search_response(response: Any) -> str:
    request = getattr(response, "request", None)
    method = str(getattr(request, "method", "") or "UNKNOWN").upper()
    status = getattr(response, "status", "unknown")
    return f"{method} {safe_goofish_url(str(getattr(response, 'url', '') or ''))} status={status}"


async def _locator_visible(page: Any, selector: str) -> bool:
    try:
        locator = page.locator(selector)
        if hasattr(locator, "first"):
            locator = locator.first
        return bool(await locator.is_visible(timeout=300))
    except Exception:
        return False


async def _page_text(page: Any) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=500)
    except Exception:
        return ""
    return " ".join(str(text).split())[:4000]


async def classify_page(page: Any) -> PageClassification:
    current_url = str(getattr(page, "url", "") or "")
    if _is_login_url(current_url):
        return PageClassification(
            failure_kind="login_required",
            reason="搜索导航进入闲鱼登录页面",
            suggestion="重新导出已登录账号状态后再运行预检",
        )

    for selector in RISK_SELECTORS:
        if await _locator_visible(page, selector):
            marker = "baxia-dialog" if "baxia" in selector else "J_MIDDLEWARE_FRAME_WIDGET"
            return PageClassification(
                failure_kind="risk_control",
                reason=f"检测到闲鱼验证页面 ({marker})",
                suggestion="停止任务并在浏览器中完成人工验证，之后重新导出账号状态",
            )

    text = await _page_text(page)
    if any(marker in text for marker in RISK_TEXT_MARKERS):
        return PageClassification(
            failure_kind="risk_control",
            reason="页面包含闲鱼安全验证提示",
            suggestion="停止任务并在浏览器中完成人工验证，之后重新导出账号状态",
        )
    if any(marker in text for marker in LOGIN_TEXT_MARKERS):
        try:
            has_password = bool(await page.locator("input[type='password']").count())
        except Exception:
            has_password = False
        if has_password or "请登录" in text or "扫码登录" in text:
            return PageClassification(
                failure_kind="login_required",
                reason="页面显示闲鱼登录界面",
                suggestion="重新导出已登录账号状态后再运行预检",
            )
    if any(marker in text for marker in ERROR_TEXT_MARKERS):
        return PageClassification(
            failure_kind="search_page_failed",
            reason="闲鱼返回错误或降级页面",
            suggestion="确认网络路径后稍后重新运行预检",
        )
    has_results = any([await _locator_visible(page, selector) for selector in RESULT_SELECTORS])
    return PageClassification(has_result_dom=has_results)


async def _safe_page_title(page: Any) -> str:
    try:
        return " ".join(str(await page.title()).split())[:160]
    except Exception:
        return ""


async def _cancel_navigation(task: asyncio.Task[Any]) -> None:
    if task.done():
        try:
            task.result()
        except BaseException:
            pass
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def navigate_search_and_capture(
    page: Any,
    search_url: str,
    *,
    timeout_ms: int = 30_000,
) -> SearchNavigationResult:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    observed: list[str] = []
    diagnostics: list[SearchResponseDiagnostic] = []

    def on_response(response: Any) -> None:
        if is_goofish_data_response(response):
            queue.put_nowait(response)

    page.on("response", on_response)
    navigation = asyncio.create_task(
        page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
    )
    deadline = time.monotonic() + timeout_ms / 1000
    navigation_error: BaseException | None = None
    last_classification = PageClassification()
    try:
        while time.monotonic() < deadline:
            last_classification = await classify_page(page)
            if last_classification.failure_kind:
                await _cancel_navigation(navigation)
                return SearchNavigationResult(
                    success=False,
                    failure_kind=last_classification.failure_kind,
                    reason=last_classification.reason,
                    suggestion=last_classification.suggestion,
                    current_url=safe_goofish_url(str(getattr(page, "url", "") or "")),
                    page_title=await _safe_page_title(page),
                    observed_requests=tuple(observed),
                    diagnostics=tuple(diagnostics),
                )

            if navigation.done() and navigation_error is None:
                try:
                    navigation.result()
                except Exception as exc:
                    navigation_error = exc

            wait_seconds = min(0.5, max(0.01, deadline - time.monotonic()))
            try:
                response = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                continue
            summary = describe_search_response(response)
            if summary not in observed and len(observed) < 20:
                observed.append(summary)
            is_search_response = is_search_results_response(response)
            try:
                payload = await response.json()
            except Exception:
                diagnostic = build_search_response_diagnostic(
                    response,
                    None,
                    is_search_response=is_search_response,
                    json_parsed=False,
                )
                if len(diagnostics) < 20:
                    diagnostics.append(diagnostic)
                if is_search_response:
                    status_classification = classify_search_response_status(
                        response,
                        None,
                    )
                    await _cancel_navigation(navigation)
                    if status_classification is not None:
                        return SearchNavigationResult(
                            success=False,
                            failure_kind=status_classification.failure_kind
                            or "search_page_failed",
                            reason=status_classification.reason,
                            suggestion=status_classification.suggestion,
                            current_url=safe_goofish_url(
                                str(getattr(page, "url", "") or "")
                            ),
                            page_title=await _safe_page_title(page),
                            observed_requests=tuple(observed),
                            diagnostics=tuple(diagnostics),
                        )
                    return SearchNavigationResult(
                        success=False,
                        failure_kind="search_parse_failed",
                        reason="闲鱼搜索数据响应不是可解析的 JSON",
                        suggestion="检查当前搜索接口响应格式后再运行预检",
                        current_url=safe_goofish_url(
                            str(getattr(page, "url", "") or "")
                        ),
                        page_title=await _safe_page_title(page),
                        observed_requests=tuple(observed),
                        diagnostics=tuple(diagnostics),
                    )
                continue
            payload_classification = classify_search_payload(payload)
            diagnostic = build_search_response_diagnostic(
                response,
                payload,
                is_search_response=is_search_response,
                marker_classification=payload_classification,
            )
            if len(diagnostics) < 20:
                diagnostics.append(diagnostic)
            if payload_classification is not None:
                await _cancel_navigation(navigation)
                return SearchNavigationResult(
                    success=False,
                    failure_kind=payload_classification.failure_kind or "search_page_failed",
                    reason=payload_classification.reason,
                    suggestion=payload_classification.suggestion,
                    current_url=safe_goofish_url(str(getattr(page, "url", "") or "")),
                    page_title=await _safe_page_title(page),
                    observed_requests=tuple(observed),
                    diagnostics=tuple(diagnostics),
                )
            if not is_search_response:
                continue
            status_classification = classify_search_response_status(
                response,
                payload,
            )
            if status_classification is not None:
                await _cancel_navigation(navigation)
                return SearchNavigationResult(
                    success=False,
                    failure_kind=status_classification.failure_kind
                    or "search_page_failed",
                    reason=status_classification.reason,
                    suggestion=status_classification.suggestion,
                    current_url=safe_goofish_url(
                        str(getattr(page, "url", "") or "")
                    ),
                    page_title=await _safe_page_title(page),
                    observed_requests=tuple(observed),
                    diagnostics=tuple(diagnostics),
                )
            normalized = normalize_search_payload(payload)
            if normalized is None:
                await _cancel_navigation(navigation)
                return SearchNavigationResult(
                    success=False,
                    failure_kind="search_parse_failed",
                    reason="闲鱼搜索数据响应缺少 resultList",
                    suggestion="检查当前搜索接口响应结构后再运行预检",
                    current_url=safe_goofish_url(
                        str(getattr(page, "url", "") or "")
                    ),
                    page_title=await _safe_page_title(page),
                    observed_requests=tuple(observed),
                    diagnostics=tuple(diagnostics),
                )
            result_list = _result_list(normalized)
            if result_list is None or not _result_list_is_parseable(result_list):
                await _cancel_navigation(navigation)
                return SearchNavigationResult(
                    success=False,
                    failure_kind="search_parse_failed",
                    reason="闲鱼搜索商品数据结构无法解析",
                    suggestion="检查当前搜索商品结构后再运行预检",
                    current_url=safe_goofish_url(
                        str(getattr(page, "url", "") or "")
                    ),
                    page_title=await _safe_page_title(page),
                    observed_requests=tuple(observed),
                    diagnostics=tuple(diagnostics),
                )
            if not navigation.done():
                try:
                    await asyncio.wait_for(asyncio.shield(navigation), timeout=5)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await _cancel_navigation(navigation)
            return SearchNavigationResult(
                success=True,
                failure_kind="success",
                reason="已识别闲鱼搜索数据源",
                suggestion="可以开始正式监控",
                current_url=safe_goofish_url(str(getattr(page, "url", "") or "")),
                page_title=await _safe_page_title(page),
                source=summary,
                response=CapturedSearchResponse(response, normalized, summary),
                observed_requests=tuple(observed),
                diagnostics=tuple(diagnostics),
                result_count=len(result_list),
            )

        last_classification = await classify_page(page)
        if last_classification.failure_kind:
            failure_kind = last_classification.failure_kind
            reason = last_classification.reason
            suggestion = last_classification.suggestion
        elif last_classification.has_result_dom:
            failure_kind = "search_response_missing"
            reason = "搜索页面已显示商品，但未识别到可解析的搜索数据响应"
            suggestion = "检查诊断中的 Goofish 请求摘要并适配当前搜索接口"
        elif navigation_error is not None:
            failure_kind = "search_page_failed"
            reason = f"搜索页面导航失败 ({type(navigation_error).__name__})"
            suggestion = "检查显式代理和闲鱼搜索域名连通性"
        else:
            failure_kind = "search_response_missing"
            reason = "搜索页面未产生可解析的商品数据响应"
            suggestion = "检查登录状态、验证页面和当前搜索接口"
        return SearchNavigationResult(
            success=False,
            failure_kind=failure_kind,
            reason=reason,
            suggestion=suggestion,
            current_url=safe_goofish_url(str(getattr(page, "url", "") or "")),
            page_title=await _safe_page_title(page),
            observed_requests=tuple(observed),
            diagnostics=tuple(diagnostics),
        )
    finally:
        await _cancel_navigation(navigation)
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
