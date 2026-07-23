"""Run a bounded browser preflight before a monitoring process starts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from src.config import LOGIN_IS_EDGE, RUN_HEADLESS, RUNNING_IN_DOCKER, STATE_FILE
from src.rotation import load_state_files
from src.services.account_strategy_service import resolve_account_runtime_plan
from src.services.browser_runtime import (
    BrowserProxyError,
    BrowserSessionError,
    BrowserSessionPlan,
    browser_init_script,
    browser_major,
    build_session_storage_script,
    load_browser_session,
    probe_proxy_endpoint,
    resolve_browser_proxy,
)
from src.services.search_navigation import (
    classify_page,
    navigate_search_and_capture,
    safe_goofish_url,
)


PREFLIGHT_CACHE_SECONDS = 300
PREFLIGHT_DIAGNOSTIC_DIR = Path("data/preflight_diagnostics")
STAGE_LABELS = {
    "snapshot_read": "账号快照读取",
    "proxy_connect": "代理与网络路径",
    "homepage": "闲鱼首页访问",
    "session_restore": "会话恢复",
    "search_page": "闲鱼搜索页访问",
    "search_source": "搜索数据源识别",
}


@dataclass
class PreflightStage:
    key: str
    label: str
    status: str = "pending"
    message: str = "等待检查"


@dataclass
class PreflightReport:
    task_id: int
    task_name: str
    success: bool = False
    failure_kind: str | None = None
    failed_stage: str | None = None
    reason: str = ""
    suggestion: str = ""
    checked_at: str = ""
    network_mode: str = "direct"
    proxy_endpoint: str = "direct"
    state_file: str = ""
    snapshot_kind: str | None = None
    cookie_count: int = 0
    local_storage_count: int = 0
    session_storage_count: int = 0
    snapshot_browser_major: int | None = None
    runtime_browser_major: int | None = None
    browser_version_note: str = ""
    search_source: str | None = None
    search_result_count: int = 0
    search_diagnostics: tuple[dict[str, Any], ...] = ()
    current_url: str = ""
    page_title: str = ""
    observed_requests: tuple[str, ...] = ()
    diagnostic_file: str | None = None
    stages: list[PreflightStage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _task_value(task: Any, name: str, default: Any = None) -> Any:
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)


def _select_state_file(task: Any) -> str:
    state_dir = os.getenv("ACCOUNT_STATE_DIR", "state")
    available = load_state_files(state_dir)
    requested = _task_value(task, "account_state_file")
    plan = resolve_account_runtime_plan(
        strategy=_task_value(task, "account_strategy"),
        account_state_file=requested,
        has_root_state_file=os.path.exists(STATE_FILE),
        available_account_files=available,
    )
    if plan["forced_account"]:
        return str(plan["forced_account"])
    if plan["prefer_root_state"]:
        return STATE_FILE
    if available:
        return available[0]
    return str(requested or STATE_FILE)


def _stages() -> list[PreflightStage]:
    return [PreflightStage(key=key, label=label) for key, label in STAGE_LABELS.items()]


def _stage(report: PreflightReport, key: str) -> PreflightStage:
    return next(stage for stage in report.stages if stage.key == key)


def _complete_stage(report: PreflightReport, key: str, message: str) -> None:
    stage = _stage(report, key)
    stage.status = "success"
    stage.message = message


def _fail_stage(
    report: PreflightReport,
    key: str,
    *,
    failure_kind: str,
    reason: str,
    suggestion: str,
) -> None:
    stage = _stage(report, key)
    stage.status = "failed"
    stage.message = reason
    report.failure_kind = failure_kind
    report.failed_stage = key
    report.reason = reason
    report.suggestion = suggestion
    failed_index = report.stages.index(stage)
    for pending in report.stages[failed_index + 1 :]:
        pending.status = "skipped"
        pending.message = "前置阶段失败，未执行"


def _launch_channel() -> str:
    if RUNNING_IN_DOCKER:
        return "chromium"
    return "msedge" if LOGIN_IS_EDGE else "chrome"


def _launch_args() -> list[str]:
    return [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-web-security",
        "--disable-features=IsolateOrigins,site-per-process",
    ]


def _expected_local_storage_keys(plan: BrowserSessionPlan) -> list[str]:
    keys: list[str] = []
    for origin in plan.storage_state.get("origins", []):
        if origin.get("origin") != plan.target_origin:
            continue
        for item in origin.get("localStorage", []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                keys.append(item["name"])
    return keys


def _cookie_keys(cookies: Any) -> set[tuple[str, str, str]]:
    if not isinstance(cookies, list):
        return set()
    keys: set[tuple[str, str, str]] = set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        domain = cookie.get("domain")
        path = cookie.get("path")
        if all(isinstance(value, str) for value in (name, domain, path)):
            keys.add((name, domain.lstrip(".").lower(), path))
    return keys


async def _verify_storage(
    context: Any,
    page: Any,
    plan: BrowserSessionPlan,
) -> bool:
    expected_cookie_keys = _cookie_keys(plan.storage_state.get("cookies"))
    restored_cookie_keys = _cookie_keys(await context.cookies())
    if not expected_cookie_keys or not expected_cookie_keys.issubset(
        restored_cookie_keys
    ):
        return False
    local_keys = _expected_local_storage_keys(plan)
    session_keys = list(plan.session_storage_by_origin.get(plan.target_origin, {}))
    return bool(
        await page.evaluate(
            """
            ({origin, localKeys, sessionKeys}) => {
                if (window.location.origin !== origin) return false;
                return localKeys.every((key) => localStorage.getItem(key) !== null)
                    && sessionKeys.every((key) => sessionStorage.getItem(key) !== null);
            }
            """,
            {
                "origin": plan.target_origin,
                "localKeys": local_keys,
                "sessionKeys": session_keys,
            },
        )
    )


def _safe_diagnostic_payload(report: PreflightReport) -> dict[str, Any]:
    return {
        "task_id": report.task_id,
        "success": report.success,
        "failure_kind": report.failure_kind,
        "failed_stage": report.failed_stage,
        "reason": report.reason,
        "suggestion": report.suggestion,
        "checked_at": report.checked_at,
        "network_mode": report.network_mode,
        "proxy_endpoint": report.proxy_endpoint,
        "snapshot_kind": report.snapshot_kind,
        "cookie_count": report.cookie_count,
        "local_storage_count": report.local_storage_count,
        "session_storage_count": report.session_storage_count,
        "snapshot_browser_major": report.snapshot_browser_major,
        "runtime_browser_major": report.runtime_browser_major,
        "browser_version_note": report.browser_version_note,
        "current_url": report.current_url,
        "page_title": report.page_title,
        "search_source": report.search_source,
        "search_result_count": report.search_result_count,
        "search_diagnostics": list(report.search_diagnostics),
        "observed_requests": list(report.observed_requests),
        "stages": [asdict(stage) for stage in report.stages],
    }


def _write_diagnostic(report: PreflightReport) -> str | None:
    try:
        task_dir = PREFLIGHT_DIAGNOSTIC_DIR / f"task_{report.task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = task_dir / f"preflight-{stamp}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(_safe_diagnostic_payload(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return str(path)
    except OSError:
        return None


class MonitoringPreflightService:
    def __init__(self, *, cache_seconds: int = PREFLIGHT_CACHE_SECONDS) -> None:
        self.cache_seconds = max(0, int(cache_seconds))
        self._reports: dict[int, tuple[str, float, PreflightReport]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _fingerprint(self, task: Any) -> str:
        state_file = _select_state_file(task)
        try:
            state_mtime = Path(state_file).stat().st_mtime_ns
        except OSError:
            state_mtime = -1
        material = "|".join(
            (
                str(_task_value(task, "id", -1)),
                str(_task_value(task, "keyword", "")),
                state_file,
                str(state_mtime),
                os.getenv("SCRAPER_PROXY_URL", "").strip() or "direct",
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get_last_report(self, task_id: int) -> PreflightReport | None:
        cached = self._reports.get(int(task_id))
        return cached[2] if cached else None

    def _cached_success(self, task: Any) -> PreflightReport | None:
        task_id = int(_task_value(task, "id", -1))
        cached = self._reports.get(task_id)
        if not cached:
            return None
        fingerprint, created_at, report = cached
        if not report.success or fingerprint != self._fingerprint(task):
            return None
        if time_monotonic() - created_at > self.cache_seconds:
            return None
        return report

    async def ensure(self, task: Any) -> PreflightReport:
        cached = self._cached_success(task)
        if cached is not None:
            return cached
        task_id = int(_task_value(task, "id", -1))
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            cached = self._cached_success(task)
            if cached is not None:
                return cached
            return await self._run_and_store(task)

    async def run(self, task: Any) -> PreflightReport:
        task_id = int(_task_value(task, "id", -1))
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            return await self._run_and_store(task)

    async def _run_and_store(self, task: Any) -> PreflightReport:
        task_id = int(_task_value(task, "id", -1))
        report = await self._run_uncached(task)
        self._reports[task_id] = (
            self._fingerprint(task),
            time_monotonic(),
            report,
        )
        return report

    async def _run_uncached(self, task: Any) -> PreflightReport:
        task_id = int(_task_value(task, "id", -1))
        task_name = str(_task_value(task, "task_name", "未命名任务"))
        keyword = str(_task_value(task, "keyword", "")).strip()
        state_file = _select_state_file(task)
        report = PreflightReport(
            task_id=task_id,
            task_name=task_name,
            checked_at=datetime.now(timezone.utc).isoformat(),
            state_file=Path(state_file).name,
            stages=_stages(),
        )

        try:
            plan = load_browser_session(state_file)
        except BrowserSessionError as exc:
            _fail_stage(
                report,
                "snapshot_read",
                failure_kind="session_incomplete",
                reason=str(exc),
                suggestion="重新导出完整的闲鱼登录状态文件",
            )
            report.diagnostic_file = _write_diagnostic(report)
            return report
        report.snapshot_kind = plan.snapshot_kind
        report.cookie_count = plan.cookie_count
        report.local_storage_count = plan.local_storage_count
        report.session_storage_count = plan.session_storage_count
        report.snapshot_browser_major = plan.snapshot_browser_major
        _complete_stage(
            report,
            "snapshot_read",
            f"已读取 {plan.snapshot_kind} 状态文件",
        )

        try:
            proxy = resolve_browser_proxy()
        except BrowserProxyError as exc:
            _fail_stage(
                report,
                "proxy_connect",
                failure_kind="proxy_unreachable",
                reason=str(exc),
                suggestion="修正本地 SCRAPER_PROXY_URL 后重新预检",
            )
            report.diagnostic_file = _write_diagnostic(report)
            return report
        report.network_mode = proxy.mode
        report.proxy_endpoint = proxy.display
        proxy_probe = await probe_proxy_endpoint(proxy)
        if not proxy_probe.success:
            _fail_stage(
                report,
                "proxy_connect",
                failure_kind="proxy_unreachable",
                reason=proxy_probe.reason,
                suggestion="确认本机代理正在运行且 Docker 可访问该端点",
            )
            report.diagnostic_file = _write_diagnostic(report)
            return report
        _complete_stage(
            report,
            "proxy_connect",
            f"{proxy_probe.reason}；模式: {proxy.display}",
        )

        browser = None
        context = None
        page = None
        try:
            async with async_playwright() as playwright:
                launch_options: dict[str, Any] = {
                    "headless": RUN_HEADLESS,
                    "args": _launch_args(),
                    "channel": _launch_channel(),
                }
                proxy_options = proxy.playwright_options()
                if proxy_options:
                    launch_options["proxy"] = proxy_options
                browser = await playwright.chromium.launch(**launch_options)
                report.runtime_browser_major = browser_major(browser.version)
                if (
                    report.snapshot_browser_major is not None
                    and report.runtime_browser_major is not None
                    and report.snapshot_browser_major != report.runtime_browser_major
                ):
                    report.browser_version_note = (
                        "快照浏览器与运行浏览器主版本不同；已记录但不阻止预检"
                    )
                else:
                    report.browser_version_note = "浏览器主版本无已知差异"

                context_options = dict(plan.context_options)
                if plan.extra_headers:
                    context_options["extra_http_headers"] = plan.extra_headers
                context = await browser.new_context(
                    storage_state=plan.storage_state,
                    **context_options,
                )
                session_script = build_session_storage_script(plan)
                if session_script:
                    await context.add_init_script(session_script)
                await context.add_init_script(browser_init_script())
                page = await context.new_page()

                try:
                    await page.goto(
                        "https://www.goofish.com/",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                except Exception as exc:
                    _fail_stage(
                        report,
                        "homepage",
                        failure_kind="network_unreachable",
                        reason=f"闲鱼首页访问失败 ({type(exc).__name__})",
                        suggestion="检查显式代理和 www.goofish.com HTTPS 连通性",
                    )
                    report.current_url = safe_goofish_url(str(getattr(page, "url", "")))
                    return report

                classification = await classify_page(page)
                report.current_url = safe_goofish_url(str(getattr(page, "url", "")))
                try:
                    report.page_title = " ".join((await page.title()).split())[:160]
                except Exception:
                    report.page_title = ""
                if classification.failure_kind:
                    _fail_stage(
                        report,
                        "homepage",
                        failure_kind=classification.failure_kind,
                        reason=classification.reason,
                        suggestion=classification.suggestion,
                    )
                    return report
                _complete_stage(report, "homepage", "闲鱼首页 HTTPS 访问成功")

                try:
                    storage_ok = await _verify_storage(context, page, plan)
                except Exception:
                    storage_ok = False
                if not storage_ok:
                    _fail_stage(
                        report,
                        "session_restore",
                        failure_kind="session_incomplete",
                        reason="Cookie 或 Web Storage 未完整恢复到 Goofish origin",
                        suggestion="重新导出增强登录快照后再次预检",
                    )
                    return report
                _complete_stage(
                    report,
                    "session_restore",
                    (
                        f"Cookie {plan.cookie_count}，localStorage "
                        f"{plan.local_storage_count}，sessionStorage "
                        f"{plan.session_storage_count} 已恢复"
                    ),
                )

                search_url = f"https://www.goofish.com/search?{urlencode({'q': keyword})}"
                search_result = await navigate_search_and_capture(page, search_url)
                report.current_url = search_result.current_url
                report.page_title = search_result.page_title
                report.observed_requests = search_result.observed_requests
                report.search_result_count = search_result.result_count
                report.search_diagnostics = tuple(
                    diagnostic.to_dict()
                    for diagnostic in search_result.diagnostics
                )
                if not search_result.success:
                    failed_stage = (
                        "search_page"
                        if search_result.failure_kind
                        in {
                            "login_required",
                            "risk_control",
                            "search_page_failed",
                        }
                        else "search_source"
                    )
                    if failed_stage == "search_source":
                        _complete_stage(report, "search_page", "搜索页面导航完成")
                    _fail_stage(
                        report,
                        failed_stage,
                        failure_kind=search_result.failure_kind,
                        reason=search_result.reason,
                        suggestion=search_result.suggestion,
                    )
                    return report
                _complete_stage(report, "search_page", "搜索页面访问成功，未进入登录或验证页")
                report.search_source = search_result.source
                if search_result.result_count:
                    source_message = (
                        "已捕获可解析的闲鱼商品数据 "
                        f"({search_result.result_count} 条)"
                    )
                else:
                    source_message = "当前筛选条件没有商品，搜索响应正常"
                _complete_stage(report, "search_source", source_message)
                report.success = True
                report.failure_kind = "success"
                report.reason = "运行环境预检通过"
                report.suggestion = "可以开始正式监控"
                return report
        except Exception as exc:
            if report.failed_stage is None:
                pending = next(
                    (stage.key for stage in report.stages if stage.status == "pending"),
                    "homepage",
                )
                _fail_stage(
                    report,
                    pending,
                    failure_kind="network_unreachable",
                    reason=f"浏览器预检失败 ({type(exc).__name__})",
                    suggestion="检查浏览器运行环境和显式代理配置",
                )
            return report
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            report.diagnostic_file = _write_diagnostic(report)


def time_monotonic() -> float:
    return time.monotonic()
