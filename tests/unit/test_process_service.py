import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.services.process_service import ProcessService


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, returncode: int = 0):
        self.returncode = returncode
        self._done.set()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


@pytest.mark.parametrize("returncode", [0, 1])
def test_process_service_marks_task_stopped_when_process_exits(
    monkeypatch, tmp_path, returncode
):
    fake_process = FakeProcess(pid=4321)
    events = []

    async def run_scenario():
        service = ProcessService()
        monkeypatch.setattr(
            "src.services.process_service.find_task_by_id_sync",
            lambda task_id: SimpleNamespace(
                id=task_id,
                task_name="task-a",
                enabled=True,
                account_state_file=None,
            ),
        )
        service.failure_guard.should_skip_start = lambda *args, **kwargs: SimpleNamespace(
            skip=False,
            should_notify=False,
            reason="",
            consecutive_failures=0,
            paused_until=None,
        )

        stopped = asyncio.Event()

        async def on_started(task_id: int):
            events.append(("started", task_id))

        async def on_stopped(task_id: int):
            events.append(("stopped", task_id))
            stopped.set()

        service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_process

        monkeypatch.setattr(
            "src.services.process_service.build_task_log_path",
            lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        started = await service.start_task(0, "task-a")
        assert started is True
        assert events == [("started", 0)]
        assert service.is_running(0) is True
        log_handle = service.log_handles[0]

        fake_process.finish(returncode)
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert ("stopped", 0) in events
        assert service.is_running(0) is False
        assert log_handle.closed is True

    asyncio.run(run_scenario())


def test_process_service_adds_debug_limit_arg_when_env_enabled(monkeypatch):
    monkeypatch.setenv("SPIDER_DEBUG_LIMIT", "1")
    service = ProcessService()

    command = service._build_spawn_command(42)

    assert command == [
        sys.executable,
        "-u",
        "spider_v2.py",
        "--task-id",
        "42",
        "--debug-limit",
        "1",
    ]


def test_process_service_uses_task_id_for_guard_and_account_lookup(monkeypatch):
    service = ProcessService()
    captured = {}

    monkeypatch.setattr(
        "src.services.process_service.find_task_by_id_sync",
        lambda task_id: SimpleNamespace(
            id=task_id,
            task_name="database-name",
            enabled=True,
            account_state_file=f"state/{task_id}.json",
        ),
    )

    def should_skip(task_key, *, cookie_path):
        captured["task_key"] = task_key
        captured["cookie_path"] = cookie_path
        return SimpleNamespace(
            skip=True,
            should_notify=False,
            reason="paused",
            consecutive_failures=1,
            paused_until=None,
        )

    service.failure_guard.should_skip_start = should_skip

    started = asyncio.run(service.start_task(84, "duplicate-name"))

    assert started is False
    assert captured == {
        "task_key": "task-id:84",
        "cookie_path": "state/84.json",
    }


def test_process_service_failed_preflight_blocks_spawn_and_failure_guard(monkeypatch):
    service = ProcessService()
    task = SimpleNamespace(
        id=23,
        task_name="task-preflight-failure",
        enabled=True,
        account_state_file="state/23.json",
    )
    report = SimpleNamespace(success=False, failed_stage="search_page")
    preflight_calls = []

    monkeypatch.setattr(
        "src.services.process_service.find_task_by_id_sync",
        lambda _task_id: task,
    )

    async def preflight_runner(received_task):
        preflight_calls.append(received_task)
        return report

    service.set_preflight_runner(preflight_runner)
    should_skip_start = Mock(
        return_value=SimpleNamespace(skip=False, should_notify=False)
    )
    record_failure = Mock()
    spawn_process = AsyncMock()
    monkeypatch.setattr(service.failure_guard, "should_skip_start", should_skip_start)
    monkeypatch.setattr(service.failure_guard, "record_failure", record_failure)
    monkeypatch.setattr(service, "_spawn_process", spawn_process)

    started = asyncio.run(service.start_task(23))

    assert started is False
    assert preflight_calls == [task]
    assert service.get_last_preflight_report(23) is report
    should_skip_start.assert_called_once_with(
        "task-id:23",
        cookie_path="state/23.json",
    )
    record_failure.assert_not_called()
    spawn_process.assert_not_awaited()
    assert service.processes == {}


def test_process_service_preflight_exception_replaces_stale_report(monkeypatch):
    service = ProcessService()
    task = SimpleNamespace(
        id=25,
        task_name="task-preflight-error",
        enabled=True,
        account_state_file="state/25.json",
    )
    service._preflight_reports[25] = {
        "success": False,
        "reason": "stale report",
    }
    monkeypatch.setattr(
        "src.services.process_service.find_task_by_id_sync",
        lambda _task_id: task,
    )
    monkeypatch.setattr(
        service.failure_guard,
        "should_skip_start",
        Mock(return_value=SimpleNamespace(skip=False, should_notify=False)),
    )
    record_failure = Mock()
    monkeypatch.setattr(service.failure_guard, "record_failure", record_failure)

    async def broken_preflight(_task):
        raise RuntimeError("fixture secret must not be returned")

    service.set_preflight_runner(broken_preflight)

    started = asyncio.run(service.start_task(25))
    report = service.get_last_preflight_report(25)

    assert started is False
    assert report["failure_kind"] == "preflight_error"
    assert report["failed_stage"] == "preflight"
    assert "fixture secret" not in report["reason"]
    assert "stale report" not in report["reason"]
    record_failure.assert_not_called()


def test_process_service_without_preflight_runner_still_starts(monkeypatch, tmp_path):
    service = ProcessService()
    task = SimpleNamespace(
        id=24,
        task_name="task-without-preflight",
        enabled=True,
        account_state_file="state/24.json",
    )
    fake_process = FakeProcess(pid=4322)
    decision = SimpleNamespace(
        skip=False,
        should_notify=False,
        reason="",
        consecutive_failures=0,
        paused_until=None,
    )
    should_skip_start = Mock(return_value=decision)
    spawn_process = AsyncMock(return_value=fake_process)

    monkeypatch.setattr(
        "src.services.process_service.find_task_by_id_sync",
        lambda _task_id: task,
    )
    monkeypatch.setattr(service.failure_guard, "should_skip_start", should_skip_start)
    monkeypatch.setattr(service, "_spawn_process", spawn_process)
    monkeypatch.setattr(
        "src.services.process_service.build_task_log_path",
        lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
    )

    async def run_scenario():
        started = await service.start_task(24)
        log_handle = service.log_handles[24]
        exit_watcher = service.exit_watchers[24]
        fake_process.finish()
        await asyncio.wait_for(exit_watcher, timeout=1)
        return started, log_handle

    assert service._preflight_runner is None
    started, log_handle = asyncio.run(run_scenario())

    assert started is True
    should_skip_start.assert_called_once_with(
        "task-id:24",
        cookie_path="state/24.json",
    )
    spawn_process.assert_awaited_once()
    assert service.is_running(24) is False
    assert log_handle.closed is True
