"""
任务管理服务
封装任务相关的业务逻辑
"""
import asyncio
from typing import List, Optional
from src.domain.models.task import Task, TaskCreate, TaskUpdate
from src.domain.repositories.task_repository import TaskRepository
from src.services.task_prompt_service import TaskPromptStore


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
        payload = task_create.model_dump()
        payload["ai_prompt_criteria_file"] = ""
        task = Task(**payload, is_running=False)
        created = await self.repository.save(task)
        if created.decision_mode != "ai" or created.id is None:
            return created

        canonical_path = self.prompt_store.criteria_path_string(created.id)
        if created.ai_prompt_criteria_file == canonical_path:
            return created
        try:
            return await self.repository.save(
                created.model_copy(update={"ai_prompt_criteria_file": canonical_path})
            )
        except BaseException:
            await self.repository.delete(created.id)
            raise

    async def create_ai_task_with_criteria(
        self,
        task_create: TaskCreate,
        generated_criteria: str,
    ) -> Task:
        created = await self.create_task(task_create)
        if created.id is None:
            raise RuntimeError("数据库未为任务分配 ID。")
        try:
            await asyncio.to_thread(
                self.prompt_store.write_criteria,
                created.id,
                generated_criteria,
            )
            return created
        except BaseException:
            await self.repository.delete(created.id)
            await asyncio.to_thread(self.prompt_store.delete_task_prompt, created.id)
            raise

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
        deleted = await self.repository.delete(task_id)
        if deleted:
            await asyncio.to_thread(self.prompt_store.delete_task_prompt, task_id)
        return deleted

    async def update_task_status(self, task_id: int, is_running: bool) -> Task:
        """更新任务运行状态"""
        task_update = TaskUpdate(is_running=is_running)
        return await self.update_task(task_id, task_update)
