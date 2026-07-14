"""
任务管理服务
封装任务相关的业务逻辑
"""
import asyncio
from typing import List, Optional
from src.domain.models.task import Task, TaskCreate, TaskUpdate
from src.domain.repositories.task_repository import TaskRepository
from src.services.task_prompt_service import TaskPromptStore


class TaskPromptIntegrityError(ValueError):
    """A newly created AI task does not have a valid criteria file."""


class TaskService:
    """任务管理服务"""

    def __init__(
        self,
        repository: TaskRepository,
        prompt_store: TaskPromptStore | None = None,
    ):
        self.repository = repository
        self.prompt_store = prompt_store or TaskPromptStore()

    async def get_all_tasks(self) -> List[Task]:
        """获取所有任务"""
        return await self.repository.find_all()

    async def get_task(self, task_id: int) -> Optional[Task]:
        """获取单个任务"""
        return await self.repository.find_by_id(task_id)

    async def create_task(self, task_create: TaskCreate) -> Task:
        """创建新任务"""
        created = await self._create_task_record(task_create)
        if created.decision_mode != "ai":
            return created

        try:
            copied = await asyncio.to_thread(
                self.prompt_store.copy_legacy_criteria,
                created.id,
                task_create.ai_prompt_criteria_file,
            )
            if not copied:
                raise TaskPromptIntegrityError(
                    "AI 任务必须提供 prompts/ 目录内有效且非空的 criteria 文件。"
                )
            return created
        except BaseException as exc:
            await self._compensate_failed_creation(created.id)
            if isinstance(exc, TaskPromptIntegrityError):
                raise
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TaskPromptIntegrityError(
                "AI 任务 criteria 复制失败，任务未创建。"
            ) from exc

    async def create_ai_task_with_criteria(
        self,
        task_create: TaskCreate,
        generated_criteria: str,
    ) -> Task:
        created = await self._create_task_record(task_create)
        try:
            await asyncio.to_thread(
                self.prompt_store.write_criteria,
                created.id,
                generated_criteria,
            )
            return created
        except BaseException:
            await self._compensate_failed_creation(created.id)
            raise

    async def _create_task_record(self, task_create: TaskCreate) -> Task:
        payload = task_create.model_dump()
        payload["ai_prompt_criteria_file"] = ""
        task = Task(**payload, is_running=False)
        created = await self.repository.save(task)
        if created.decision_mode != "ai":
            return created
        if created.id is None:
            raise RuntimeError("数据库未为任务分配 ID。")

        canonical_path = self.prompt_store.criteria_path_string(created.id)
        if created.ai_prompt_criteria_file == canonical_path:
            return created
        try:
            return await self.repository.save(
                created.model_copy(
                    update={"ai_prompt_criteria_file": canonical_path}
                )
            )
        except BaseException:
            await self._compensate_failed_creation(created.id)
            raise

    async def _compensate_failed_creation(self, task_id: int) -> None:
        try:
            await self.repository.delete(task_id)
        except BaseException as exc:
            self._log_cleanup_failure(task_id, "数据库任务", exc)
        try:
            await asyncio.to_thread(self.prompt_store.delete_task_prompt, task_id)
        except BaseException as exc:
            self._log_cleanup_failure(task_id, "Prompt 目录", exc)

    @staticmethod
    def _log_cleanup_failure(
        task_id: int,
        resource: str,
        exc: BaseException,
    ) -> None:
        print(
            f"[TaskCleanup] 任务 ID {task_id} 清理{resource}失败 "
            f"({type(exc).__name__})，已保留原始错误。"
        )

    async def update_task(self, task_id: int, task_update: TaskUpdate) -> Task:
        """更新任务"""
        task = await self.repository.find_by_id(task_id)
        if not task:
            raise ValueError(f"任务 {task_id} 不存在")

        updated_task = task.apply_update(task_update)
        if updated_task.decision_mode == "ai" and updated_task.id is not None:
            updated_task = updated_task.model_copy(
                update={
                    "ai_prompt_criteria_file": self.prompt_store.criteria_path_string(
                        updated_task.id
                    )
                }
            )
        else:
            updated_task = updated_task.model_copy(
                update={
                    "ai_prompt_criteria_file": task.ai_prompt_criteria_file,
                }
            )
        return await self.repository.save(updated_task)

    async def update_task_with_generated_criteria(
        self,
        task_id: int,
        task_update: TaskUpdate,
        generated_criteria: str,
    ) -> Task:
        previous_content = await asyncio.to_thread(
            self.prompt_store.read_criteria,
            task_id,
        )
        await asyncio.to_thread(
            self.prompt_store.write_criteria,
            task_id,
            generated_criteria,
        )
        try:
            task_update.ai_prompt_criteria_file = (
                self.prompt_store.criteria_path_string(task_id)
            )
            return await self.update_task(task_id, task_update)
        except BaseException:
            await asyncio.to_thread(
                self.prompt_store.restore_criteria,
                task_id,
                previous_content,
            )
            raise

    async def delete_task(self, task_id: int) -> bool:
        """删除任务"""
        deleted = await self.delete_task_record(task_id)
        if not deleted:
            return False
        try:
            await self.delete_task_prompt(task_id)
        except BaseException as exc:
            self._log_cleanup_failure(task_id, "Prompt 目录", exc)
        return deleted

    async def delete_task_record(self, task_id: int) -> bool:
        """Delete only the durable task row."""
        return await self.repository.delete(task_id)

    async def delete_task_prompt(self, task_id: int) -> None:
        """Delete only the canonical task Prompt directory."""
        await asyncio.to_thread(self.prompt_store.delete_task_prompt, task_id)

    async def update_task_status(self, task_id: int, is_running: bool) -> Task:
        """更新任务运行状态"""
        task_update = TaskUpdate(is_running=is_running)
        return await self.update_task(task_id, task_update)
