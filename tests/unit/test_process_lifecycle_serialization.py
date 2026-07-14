import asyncio
import io
from types import SimpleNamespace

import pytest

from src.api.routes import tasks as tasks_route
from src.services.process_service import ProcessService
from src.services.scheduler_service import SchedulerService


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


class InMemoryTaskService:
    def __init__(self, task):
        self.task = task
        self.prompt_deletes = []

    async def get_task(self, task_id: int):
        if self.task is None or self.task.id != task_id:
            return None
        return self.task

    async def delete_task_record(self, task_id: int) -> bool:
        if self.task is None or self.task.id != task_id:
            return False
        self.task = None
        return True

    async def delete_task_prompt(self, task_id: int) -> None:
        self.prompt_deletes.append(task_id)

    async def get_all_tasks(self):
        return [] if self.task is None else [self.task]


class FakeScheduler:
    def __init__(self):
        self.reload_calls = 0

    async def reload_jobs(self, _tasks):
        self.reload_calls += 1


def _task(task_id: int, name: str, *, enabled: bool = True):
    return SimpleNamespace(
        id=task_id,
        task_name=name,
        enabled=enabled,
        account_state_file=None,
    )


def _allow_start(service: ProcessService) -> None:
    service.failure_guard.should_skip_start = lambda *_args, **_kwargs: SimpleNamespace(
        skip=False,
        should_notify=False,
        reason="",
        consecutive_failures=0,
        paused_until=None,
    )
    service._append_stop_marker = lambda _path: None


def _patch_delete_cleanup(monkeypatch) -> None:
    async def delete_results(_task_id):
        return 0

    async def delete_log(_task_id, _task_name):
        return None

    monkeypatch.setattr(tasks_route, "delete_task_result_records", delete_results)
    monkeypatch.setattr(tasks_route, "delete_task_price_snapshots", lambda _task_id: 0)
    monkeypatch.setattr(
        tasks_route,
        "delete_task_result_blacklist_rules",
        lambda _task_id: 0,
    )
    monkeypatch.setattr(tasks_route, "_delete_task_log", delete_log)


def test_delete_waits_for_blocked_spawn_then_stops_registered_process(monkeypatch):
    async def scenario():
        task = _task(11, "database task")
        task_service = InMemoryTaskService(task)
        scheduler = FakeScheduler()
        process_service = ProcessService()
        _allow_start(process_service)
        _patch_delete_cleanup(monkeypatch)
        monkeypatch.setattr(
            "src.services.process_service.find_task_by_id_sync",
            lambda task_id: task_service.task if task_id == 11 else None,
        )

        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()
        process = FakeProcess(pid=1100)
        log_handle = io.StringIO()
        process_service._open_log_file = lambda *_args: ("fictional.log", log_handle)

        async def blocked_spawn(_task_id, _log_handle):
            spawn_entered.set()
            await release_spawn.wait()
            return process

        async def terminate(fake_process, _task_id):
            fake_process.finish(-15)
            await fake_process.wait()

        process_service._spawn_process = blocked_spawn
        process_service._terminate_process = terminate

        start_future = asyncio.create_task(
            process_service.start_task(11, "stale caller name")
        )
        await asyncio.wait_for(spawn_entered.wait(), timeout=1)
        delete_future = asyncio.create_task(
            tasks_route.delete_task(
                11,
                service=task_service,
                process_service=process_service,
                scheduler_service=scheduler,
            )
        )
        await asyncio.sleep(0)
        assert delete_future.done() is False

        release_spawn.set()
        assert await asyncio.wait_for(start_future, timeout=1) is True
        response = await asyncio.wait_for(delete_future, timeout=1)

        assert response == {"message": "任务删除成功"}
        assert task_service.task is None
        assert process_service.is_running(11) is False
        assert log_handle.closed is True
        assert scheduler.reload_calls == 1

    asyncio.run(scenario())


def test_start_arriving_during_delete_rechecks_database_after_guard(monkeypatch):
    async def scenario():
        task_service = InMemoryTaskService(_task(12, "delete first"))
        scheduler = FakeScheduler()
        process_service = ProcessService()
        _allow_start(process_service)
        _patch_delete_cleanup(monkeypatch)
        monkeypatch.setattr(
            "src.services.process_service.find_task_by_id_sync",
            lambda task_id: task_service.task if task_id == 12 else None,
        )

        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()
        spawn_calls = 0

        async def blocked_stop(_task_id):
            stop_entered.set()
            await release_stop.wait()
            return False

        async def forbidden_spawn(_task_id, _log_handle):
            nonlocal spawn_calls
            spawn_calls += 1
            raise AssertionError("stale start must not spawn")

        process_service._stop_task_locked = blocked_stop
        process_service._spawn_process = forbidden_spawn

        delete_future = asyncio.create_task(
            tasks_route.delete_task(
                12,
                service=task_service,
                process_service=process_service,
                scheduler_service=scheduler,
            )
        )
        await asyncio.wait_for(stop_entered.wait(), timeout=1)
        stale_start = asyncio.create_task(
            process_service.start_task(12, "stale scheduler name")
        )
        await asyncio.sleep(0)
        assert stale_start.done() is False

        release_stop.set()
        assert await asyncio.wait_for(delete_future, timeout=1) == {
            "message": "任务删除成功"
        }
        assert await asyncio.wait_for(stale_start, timeout=1) is False
        assert spawn_calls == 0
        assert task_service.task is None

    asyncio.run(scenario())


def test_cancelled_delete_holds_guard_until_durable_row_delete_settles(monkeypatch):
    async def scenario():
        task_service = InMemoryTaskService(_task(13, "cancel delete"))
        scheduler = FakeScheduler()
        process_service = ProcessService()
        _allow_start(process_service)
        _patch_delete_cleanup(monkeypatch)
        monkeypatch.setattr(
            "src.services.process_service.find_task_by_id_sync",
            lambda task_id: task_service.task if task_id == 13 else None,
        )

        delete_entered = asyncio.Event()
        release_delete = asyncio.Event()
        spawn_calls = 0

        async def blocked_delete(task_id):
            delete_entered.set()
            await release_delete.wait()
            task_service.task = None
            return True

        async def forbidden_spawn(_task_id, _log_handle):
            nonlocal spawn_calls
            spawn_calls += 1
            raise AssertionError("start must observe the committed deletion")

        task_service.delete_task_record = blocked_delete
        process_service._spawn_process = forbidden_spawn

        delete_future = asyncio.create_task(
            tasks_route.delete_task(
                13,
                service=task_service,
                process_service=process_service,
                scheduler_service=scheduler,
            )
        )
        await asyncio.wait_for(delete_entered.wait(), timeout=1)
        delete_future.cancel()
        await asyncio.sleep(0)

        stale_start = asyncio.create_task(
            process_service.start_task(13, "stale scheduler name")
        )
        await asyncio.sleep(0)
        assert delete_future.done() is False
        assert stale_start.done() is False

        release_delete.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(delete_future, timeout=1)
        assert await asyncio.wait_for(stale_start, timeout=1) is False
        assert task_service.task is None
        assert spawn_calls == 0

    asyncio.run(scenario())


def test_stale_scheduler_job_does_not_open_log_or_spawn(monkeypatch):
    service = ProcessService()
    scheduler = SchedulerService(service)
    monkeypatch.setattr(
        "src.services.process_service.find_task_by_id_sync",
        lambda _task_id: None,
    )
    service._open_log_file = lambda *_args: (_ for _ in ()).throw(
        AssertionError("log must not open")
    )

    async def forbidden_spawn(_task_id, _log_handle):
        raise AssertionError("subprocess must not spawn")

    service._spawn_process = forbidden_spawn

    assert asyncio.run(scheduler._run_task(99, "stale job")) is None
    assert service.is_running(99) is False


def test_task_lifecycle_locks_do_not_block_other_task_ids(monkeypatch):
    async def scenario():
        tasks = {21: _task(21, "database A"), 22: _task(22, "database B")}
        service = ProcessService()
        _allow_start(service)
        monkeypatch.setattr(
            "src.services.process_service.find_task_by_id_sync",
            lambda task_id: tasks.get(task_id),
        )
        release_a = asyncio.Event()
        entered_a = asyncio.Event()
        processes = {21: FakeProcess(2100), 22: FakeProcess(2200)}
        handles = {}

        def open_log(task_id, task_name):
            handles[task_id] = io.StringIO()
            return f"{task_name}.log", handles[task_id]

        async def spawn(task_id, _handle):
            if task_id == 21:
                entered_a.set()
                await release_a.wait()
            return processes[task_id]

        async def terminate(process, _task_id):
            process.finish(-15)
            await process.wait()

        service._open_log_file = open_log
        service._spawn_process = spawn
        service._terminate_process = terminate

        start_a = asyncio.create_task(service.start_task(21, "stale A"))
        await asyncio.wait_for(entered_a.wait(), timeout=1)
        assert await asyncio.wait_for(
            service.start_task(22, "stale B"),
            timeout=1,
        ) is True
        assert service.task_names[22] == "database B"
        assert start_a.done() is False

        release_a.set()
        assert await asyncio.wait_for(start_a, timeout=1) is True
        assert service.task_names[21] == "database A"
        assert await service.stop_task(21) is True
        assert await service.stop_task(22) is True

    asyncio.run(scenario())


def test_runtime_registration_failure_terminates_process_and_closes_log(monkeypatch):
    async def scenario():
        service = ProcessService()
        _allow_start(service)
        monkeypatch.setattr(
            "src.services.process_service.find_task_by_id_sync",
            lambda task_id: _task(task_id, "database task"),
        )
        process = FakeProcess(pid=3100)
        log_handle = io.StringIO()
        terminated = []
        service._open_log_file = lambda *_args: ("fictional.log", log_handle)

        async def spawn(_task_id, _handle):
            return process

        async def terminate(fake_process, task_id):
            terminated.append(task_id)
            fake_process.finish(-15)
            await fake_process.wait()

        def fail_registration(*_args):
            raise RuntimeError("fictional registration failure")

        service._spawn_process = spawn
        service._terminate_process = terminate
        service._register_runtime = fail_registration

        assert await service.start_task(31, "stale name") is False
        assert terminated == [31]
        assert process.returncode == -15
        assert log_handle.closed is True
        assert service.is_running(31) is False

    asyncio.run(scenario())


def test_fast_process_exit_waits_for_started_hook_before_stopped_hook(monkeypatch):
    async def scenario():
        service = ProcessService()
        _allow_start(service)
        monkeypatch.setattr(
            "src.services.process_service.find_task_by_id_sync",
            lambda task_id: _task(task_id, "database task"),
        )
        process = FakeProcess(pid=3200)
        log_handle = io.StringIO()
        started_entered = asyncio.Event()
        release_started = asyncio.Event()
        stopped = asyncio.Event()
        events = []

        service._open_log_file = lambda *_args: ("fictional.log", log_handle)

        async def spawn(_task_id, _handle):
            return process

        async def on_started(_task_id):
            events.append("started-begin")
            started_entered.set()
            await release_started.wait()
            events.append("started-complete")

        async def on_stopped(_task_id):
            events.append("stopped")
            stopped.set()

        service._spawn_process = spawn
        service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)

        start_future = asyncio.create_task(service.start_task(32, "stale name"))
        await asyncio.wait_for(started_entered.wait(), timeout=1)
        process.finish(0)
        await asyncio.sleep(0)

        assert events == ["started-begin"]
        assert service.processes[32] is process

        release_started.set()
        assert await asyncio.wait_for(start_future, timeout=1) is True
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert events == ["started-begin", "started-complete", "stopped"]
        assert service.is_running(32) is False
        assert log_handle.closed is True

    asyncio.run(scenario())


def test_cancelled_start_terminates_registered_process_before_releasing_guard(
    monkeypatch,
):
    async def scenario():
        service = ProcessService()
        _allow_start(service)
        monkeypatch.setattr(
            "src.services.process_service.find_task_by_id_sync",
            lambda task_id: _task(task_id, "database task"),
        )
        process = FakeProcess(pid=3300)
        log_handle = io.StringIO()
        started_entered = asyncio.Event()
        stopped = asyncio.Event()
        terminated = []

        service._open_log_file = lambda *_args: ("fictional.log", log_handle)

        async def spawn(_task_id, _handle):
            return process

        async def terminate(fake_process, task_id):
            terminated.append(task_id)
            fake_process.finish(-15)
            await fake_process.wait()

        async def on_started(_task_id):
            started_entered.set()
            await asyncio.Event().wait()

        async def on_stopped(_task_id):
            stopped.set()

        service._spawn_process = spawn
        service._terminate_process = terminate
        service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)

        start_future = asyncio.create_task(service.start_task(33, "stale name"))
        await asyncio.wait_for(started_entered.wait(), timeout=1)
        start_future.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start_future, timeout=1)

        assert terminated == [33]
        assert stopped.is_set()
        assert service.is_running(33) is False
        assert log_handle.closed is True

    asyncio.run(scenario())


def test_missing_or_disabled_task_is_rejected_before_opening_log(monkeypatch):
    for task in (None, _task(41, "disabled", enabled=False)):
        service = ProcessService()
        monkeypatch.setattr(
            "src.services.process_service.find_task_by_id_sync",
            lambda _task_id, current=task: current,
        )
        service._open_log_file = lambda *_args: (_ for _ in ()).throw(
            AssertionError("log must not open")
        )

        assert asyncio.run(service.start_task(41, "caller name")) is False
