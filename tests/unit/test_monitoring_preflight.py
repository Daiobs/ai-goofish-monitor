import asyncio
from types import SimpleNamespace

from src.services.monitoring_preflight import (
    MonitoringPreflightService,
    PreflightReport,
    _verify_storage,
)
from src.services.browser_runtime import BrowserSessionError


def test_monitoring_preflight_reuses_cached_success(monkeypatch):
    service = MonitoringPreflightService(cache_seconds=300)
    task = SimpleNamespace(id=17, task_name="task-a", keyword="macbook")
    report = PreflightReport(task_id=17, task_name="task-a", success=True)
    uncached_calls = []

    async def fake_run_uncached(received_task):
        uncached_calls.append(received_task)
        return report

    monkeypatch.setattr(service, "_run_uncached", fake_run_uncached)
    monkeypatch.setattr(service, "_fingerprint", lambda _task: "stable-fingerprint")
    monkeypatch.setattr(
        "src.services.monitoring_preflight.time_monotonic",
        lambda: 100.0,
    )

    async def run_scenario():
        first = await service.ensure(task)
        second = await service.ensure(task)
        return first, second

    first, second = asyncio.run(run_scenario())

    assert first is report
    assert second is first
    assert uncached_calls == [task]


def test_monitoring_preflight_reports_malformed_snapshot_without_browser(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    service = MonitoringPreflightService(cache_seconds=0)
    task = SimpleNamespace(id=18, task_name="task-b", keyword="camera")
    browser_started = []

    monkeypatch.setattr(
        "src.services.monitoring_preflight._select_state_file",
        lambda _task: "state/broken.json",
    )
    monkeypatch.setattr(
        "src.services.monitoring_preflight.load_browser_session",
        lambda _path: (_ for _ in ()).throw(
            BrowserSessionError("登录状态文件顶层必须是 object")
        ),
    )
    monkeypatch.setattr(
        "src.services.monitoring_preflight.async_playwright",
        lambda: browser_started.append(True),
    )

    report = asyncio.run(service.run(task))

    assert report.success is False
    assert report.failure_kind == "session_incomplete"
    assert report.failed_stage == "snapshot_read"
    assert report.stages[0].status == "failed"
    assert all(stage.status == "skipped" for stage in report.stages[1:])
    assert browser_started == []


def test_verify_storage_requires_cookie_and_web_storage_keys():
    cookie = {
        "name": "session",
        "domain": ".goofish.com",
        "path": "/",
        "value": "fixture-value",
    }
    plan = SimpleNamespace(
        target_origin="https://www.goofish.com",
        storage_state={
            "cookies": [cookie],
            "origins": [
                {
                    "origin": "https://www.goofish.com",
                    "localStorage": [{"name": "account", "value": "fixture"}],
                }
            ],
        },
        session_storage_by_origin={
            "https://www.goofish.com": {"view": "grid"}
        },
    )

    class FakeContext:
        async def cookies(self):
            return [dict(cookie)]

    class FakePage:
        async def evaluate(self, _script, values):
            return values == {
                "origin": "https://www.goofish.com",
                "localKeys": ["account"],
                "sessionKeys": ["view"],
            }

    assert asyncio.run(_verify_storage(FakeContext(), FakePage(), plan)) is True

    class MissingCookieContext:
        async def cookies(self):
            return []

    assert (
        asyncio.run(_verify_storage(MissingCookieContext(), FakePage(), plan))
        is False
    )
