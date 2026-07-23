"""
进程管理服务
负责管理爬虫进程的启动和停止
"""

import asyncio
import contextlib
import os
import signal
import sys
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, TextIO

from src.ai_handler import send_ntfy_notification
from src.config import STATE_FILE
from src.failure_guard import FailureGuard, canonical_task_key
from src.infrastructure.persistence.sqlite_task_repository import find_task_by_id_sync
from src.utils import build_task_log_path

STOP_TIMEOUT_SECONDS = 20
SPIDER_DEBUG_LIMIT_ENV = "SPIDER_DEBUG_LIMIT"
LifecycleHook = Callable[[int], Awaitable[None] | None]
PreflightRunner = Callable[[Any], Awaitable[Any]]


class ProcessService:
    """进程管理服务"""

    def __init__(self):
        self.processes: Dict[int, asyncio.subprocess.Process] = {}
        self.log_paths: Dict[int, str] = {}
        self.log_handles: Dict[int, TextIO] = {}
        self.task_names: Dict[int, str] = {}
        self.exit_watchers: Dict[int, asyncio.Task] = {}
        self._task_lifecycle_locks: Dict[int, asyncio.Lock] = {}
        self.failure_guard = FailureGuard()
        self._on_started: LifecycleHook | None = None
        self._on_stopped: LifecycleHook | None = None
        self._preflight_runner: PreflightRunner | None = None
        self._preflight_reports: Dict[int, Any] = {}

    def set_lifecycle_hooks(
        self,
        *,
        on_started: LifecycleHook | None = None,
        on_stopped: LifecycleHook | None = None,
    ) -> None:
        self._on_started = on_started
        self._on_stopped = on_stopped

    def set_preflight_runner(self, runner: PreflightRunner | None) -> None:
        self._preflight_runner = runner

    def get_last_preflight_report(self, task_id: int) -> Any | None:
        return self._preflight_reports.get(task_id)

    async def _invoke_hook(self, hook: LifecycleHook | None, task_id: int) -> None:
        if hook is None:
            return
        result = hook(task_id)
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    async def _complete_cleanup(awaitable: Awaitable[None]) -> None:
        """Finish critical cleanup even if the caller receives another cancel."""
        cleanup_task = asyncio.create_task(awaitable)
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
        cleanup_task.result()

    def task_lifecycle_guard(self, task_id: int) -> asyncio.Lock:
        """Return the persistent per-task lock used by start, stop, and delete."""
        lock = self._task_lifecycle_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._task_lifecycle_locks[task_id] = lock
        return lock

    @staticmethod
    def _resolve_cookie_path(task) -> str | None:
        """Best-effort cookie/state path from the freshly loaded task."""
        account_state_file = getattr(task, "account_state_file", None)
        if isinstance(account_state_file, str) and account_state_file.strip():
            return account_state_file.strip()

        return STATE_FILE if os.path.exists(STATE_FILE) else None

    def is_running(self, task_id: int) -> bool:
        """检查任务是否正在运行"""
        process = self.processes.get(task_id)
        return process is not None and process.returncode is None

    async def _drain_finished_process(self, task_id: int) -> None:
        process = self.processes.get(task_id)
        if process is None or process.returncode is None:
            return
        await self._finalize_process_exit_locked(task_id, process)

    def _open_log_file(self, task_id: int, task_name: str) -> tuple[str, TextIO]:
        os.makedirs("logs", exist_ok=True)
        log_file_path = build_task_log_path(task_id, task_name)
        log_file_handle = open(log_file_path, "a", encoding="utf-8")
        return log_file_path, log_file_handle

    def _build_spawn_command(self, task_id: int) -> list[str]:
        command = [
            sys.executable,
            "-u",
            "spider_v2.py",
            "--task-id",
            str(task_id),
        ]
        debug_limit = str(os.getenv(SPIDER_DEBUG_LIMIT_ENV, "")).strip()
        if debug_limit.isdigit() and int(debug_limit) > 0:
            command.extend(["--debug-limit", debug_limit])
        return command

    async def _spawn_process(
        self,
        task_id: int,
        log_file_handle: TextIO,
    ) -> asyncio.subprocess.Process:
        preexec_fn = os.setsid if sys.platform != "win32" else None
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        return await asyncio.create_subprocess_exec(
            *self._build_spawn_command(task_id),
            stdout=log_file_handle,
            stderr=log_file_handle,
            preexec_fn=preexec_fn,
            env=child_env,
        )

    def _register_runtime(
        self,
        task_id: int,
        task_name: str,
        process: asyncio.subprocess.Process,
        log_file_path: str,
        log_file_handle: TextIO,
        startup_complete: asyncio.Event,
    ) -> None:
        self.processes[task_id] = process
        self.log_paths[task_id] = log_file_path
        self.log_handles[task_id] = log_file_handle
        self.task_names[task_id] = task_name
        self.exit_watchers[task_id] = asyncio.create_task(
            self._watch_process_exit(task_id, process, startup_complete)
        )

    async def start_task(self, task_id: int, task_name: str | None = None) -> bool:
        """Start a task while serializing its complete lifecycle transition."""
        async with self.task_lifecycle_guard(task_id):
            return await self._start_task_locked(task_id)

    async def _start_task_locked(self, task_id: int) -> bool:
        """Start a task while its lifecycle lock is already held."""
        try:
            task = await asyncio.to_thread(find_task_by_id_sync, task_id)
        except Exception as exc:
            print(
                f"任务 ID {task_id} 启动前读取失败 "
                f"({type(exc).__name__})"
            )
            return False
        if task is None:
            print(f"任务 ID {task_id} 已不存在，拒绝启动")
            return False
        if not task.enabled:
            print(f"任务 ID {task_id} 已禁用，拒绝启动")
            return False

        task_name = task.task_name
        await self._drain_finished_process(task_id)
        if self.is_running(task_id):
            print(f"任务 '{task_name}' (ID: {task_id}) 已在运行中")
            return False

        decision = self.failure_guard.should_skip_start(
            canonical_task_key(task_id),
            cookie_path=self._resolve_cookie_path(task),
        )
        if decision.skip:
            await self._notify_skip(task_name, decision)
            return False

        if self._preflight_runner is not None:
            self._preflight_reports.pop(task_id, None)
            try:
                preflight_report = await self._preflight_runner(task)
            except Exception as exc:
                self._preflight_reports[task_id] = {
                    "task_id": task_id,
                    "success": False,
                    "failure_kind": "preflight_error",
                    "failed_stage": "preflight",
                    "reason": f"运行环境预检异常 ({type(exc).__name__})",
                    "suggestion": "检查浏览器运行环境后重新预检",
                    "stages": [],
                }
                print(
                    f"任务 ID {task_id} 运行环境预检异常 "
                    f"({type(exc).__name__})"
                )
                return False
            self._preflight_reports[task_id] = preflight_report
            preflight_success = bool(
                preflight_report.get("success")
                if isinstance(preflight_report, dict)
                else getattr(preflight_report, "success", False)
            )
            if not preflight_success:
                failed_stage = (
                    preflight_report.get("failed_stage")
                    if isinstance(preflight_report, dict)
                    else getattr(preflight_report, "failed_stage", "unknown")
                )
                print(f"任务 ID {task_id} 预检未通过 (stage={failed_stage or 'unknown'})")
                return False

        log_file_path = ""
        log_file_handle = None
        process = None
        startup_complete = asyncio.Event()
        started_hook_invoked = False
        try:
            log_file_path, log_file_handle = self._open_log_file(task_id, task_name)
            process = await self._spawn_process(task_id, log_file_handle)
            self._register_runtime(
                task_id,
                task_name,
                process,
                log_file_path,
                log_file_handle,
                startup_complete,
            )
            print(f"启动任务 '{task_name}' (PID: {process.pid})")
            started_hook_invoked = True
            await self._invoke_hook(self._on_started, task_id)
            startup_complete.set()
            return True
        except BaseException as exc:
            try:
                await self._complete_cleanup(
                    self._compensate_failed_start(
                        task_id,
                        process,
                        log_file_handle,
                        notify_stopped=started_hook_invoked,
                    )
                )
            except BaseException as cleanup_exc:
                print(
                    f"任务 ID {task_id} 启动补偿失败 "
                    f"({type(cleanup_exc).__name__})"
                )
            finally:
                startup_complete.set()
            if not isinstance(exc, Exception):
                raise
            print(
                f"启动任务 '{task_name}' 失败 "
                f"({type(exc).__name__})"
            )
            return False

    async def _compensate_failed_start(
        self,
        task_id: int,
        process: asyncio.subprocess.Process | None,
        log_file_handle: TextIO | None,
        *,
        notify_stopped: bool,
    ) -> None:
        if process is not None and process.returncode is None:
            try:
                await self._terminate_process(process, task_id)
            except Exception as cleanup_exc:
                print(
                    f"任务 ID {task_id} 启动补偿终止失败 "
                    f"({type(cleanup_exc).__name__})"
                )

        if process is not None and process.returncode is not None:
            if self.processes.get(task_id) is process:
                self._cleanup_runtime(task_id, process)
                if notify_stopped:
                    try:
                        await self._invoke_hook(self._on_stopped, task_id)
                    except Exception as cleanup_exc:
                        print(
                            f"任务 ID {task_id} 启动补偿状态收尾失败 "
                            f"({type(cleanup_exc).__name__})"
                        )

        if self.log_handles.get(task_id) is not log_file_handle:
            self._close_log_handle(log_file_handle)

    async def _notify_skip(self, task_name: str, decision) -> None:
        print(
            f"[FailureGuard] 跳过启动任务 '{task_name}'，已暂停重试 "
            f"(连续失败 {decision.consecutive_failures}/{self.failure_guard.threshold})"
        )
        if not decision.should_notify:
            return
        try:
            await send_ntfy_notification(
                {
                    "商品标题": f"[任务暂停] {task_name}",
                    "当前售价": "N/A",
                    "商品链接": "#",
                },
                "任务处于暂停状态，将跳过执行。\n"
                f"原因: {decision.reason}\n"
                f"连续失败: {decision.consecutive_failures}/{self.failure_guard.threshold}\n"
                f"暂停到: {decision.paused_until.strftime('%Y-%m-%d %H:%M:%S') if decision.paused_until else 'N/A'}\n"
                "修复方法: 更新登录态/cookies文件后会自动恢复。",
            )
        except Exception as exc:
            print(f"发送任务暂停通知失败: {exc}")

    async def _watch_process_exit(
        self,
        task_id: int,
        process: asyncio.subprocess.Process,
        startup_complete: asyncio.Event,
    ) -> None:
        await process.wait()
        await startup_complete.wait()
        async with self.task_lifecycle_guard(task_id):
            await self._finalize_process_exit_locked(task_id, process)

    async def _finalize_process_exit_locked(
        self,
        task_id: int,
        process: asyncio.subprocess.Process,
    ) -> None:
        if self.processes.get(task_id) is not process:
            return
        self._cleanup_runtime(task_id, process)
        await self._invoke_hook(self._on_stopped, task_id)

    def _cleanup_runtime(
        self,
        task_id: int,
        process: asyncio.subprocess.Process,
    ) -> None:
        if self.processes.get(task_id) is not process:
            return
        self.processes.pop(task_id, None)
        self.log_paths.pop(task_id, None)
        self.task_names.pop(task_id, None)
        self._close_log_handle(self.log_handles.pop(task_id, None))
        self.exit_watchers.pop(task_id, None)

    def _close_log_handle(self, log_handle: TextIO | None) -> None:
        if log_handle is None:
            return
        with contextlib.suppress(Exception):
            log_handle.close()

    def _append_stop_marker(self, log_path: str | None) -> None:
        if not log_path:
            return
        try:
            timestamp = datetime.now().strftime(" %Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"[{timestamp}] !!! 任务已被终止 !!!\n")
        except Exception as exc:
            print(f"写入任务终止标记失败: {exc}")

    async def stop_task(self, task_id: int) -> bool:
        """Stop a task while serializing its complete lifecycle transition."""
        async with self.task_lifecycle_guard(task_id):
            return await self._stop_task_locked(task_id)

    async def _stop_task_locked(self, task_id: int) -> bool:
        """Stop a task while its lifecycle lock is already held."""
        await self._drain_finished_process(task_id)
        process = self.processes.get(task_id)
        if process is None:
            print(f"任务 ID {task_id} 没有正在运行的进程")
            return False
        if process.returncode is not None:
            await self._finalize_process_exit_locked(task_id, process)
            print(f"任务进程 {process.pid} (ID: {task_id}) 已退出，略过停止")
            return False

        try:
            await self._terminate_process(process, task_id)
            self._append_stop_marker(self.log_paths.get(task_id))
            await self._finalize_process_exit_locked(task_id, process)
            print(f"任务进程 {process.pid} (ID: {task_id}) 已终止")
            return True
        except ProcessLookupError:
            print(f"进程 (ID: {task_id}) 已不存在")
            return False
        except Exception as exc:
            print(f"停止任务进程 (ID: {task_id}) 时出错: {exc}")
            return False

    async def _terminate_process(
        self,
        process: asyncio.subprocess.Process,
        task_id: int,
    ) -> None:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()

        try:
            await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT_SECONDS)
            return
        except asyncio.TimeoutError:
            print(
                f"任务进程 {process.pid} (ID: {task_id}) 未在 "
                f"{STOP_TIMEOUT_SECONDS} 秒内退出，准备强制终止..."
            )

        if sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
        await process.wait()

    async def stop_all(self) -> None:
        """停止所有任务进程"""
        task_ids = list(self.processes.keys())
        for task_id in task_ids:
            await self.stop_task(task_id)
