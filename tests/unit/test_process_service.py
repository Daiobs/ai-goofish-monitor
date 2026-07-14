import asyncio
import sys
from types import SimpleNamespace

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
