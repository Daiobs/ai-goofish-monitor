"""
基于JSON文件的任务仓储实现
"""
from typing import List, Optional
import json
import aiofiles
from src.domain.models.task import Task
from src.domain.repositories.task_repository import TaskRepository


class JsonTaskRepository(TaskRepository):
    """只读旧配置适配器；可写任务统一存储在 SQLite。"""

    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file

    async def find_all(self) -> List[Task]:
        """获取所有任务"""
        try:
            async with aiofiles.open(self.config_file, 'r', encoding='utf-8') as f:
                content = await f.read()
                if not content.strip():
                    return []

                tasks_data = json.loads(content)
                tasks = []
                reserved_ids = {
                    int(task_data["id"])
                    for task_data in tasks_data
                    if isinstance(task_data, dict)
                    and not isinstance(task_data.get("id"), bool)
                    and str(task_data.get("id", "")).isdigit()
                }
                next_fallback_id = 0
                assigned_ids = set()
                for task_data in tasks_data:
                    if not isinstance(task_data, dict):
                        continue
                    payload = dict(task_data)
                    raw_id = payload.get("id")
                    parsed_id = (
                        int(raw_id)
                        if not isinstance(raw_id, bool)
                        and raw_id is not None
                        and str(raw_id).isdigit()
                        else None
                    )
                    if parsed_id is None or parsed_id in assigned_ids:
                        while next_fallback_id in reserved_ids:
                            next_fallback_id += 1
                        parsed_id = next_fallback_id
                        reserved_ids.add(parsed_id)
                        next_fallback_id += 1
                    assigned_ids.add(parsed_id)
                    payload["id"] = parsed_id
                    tasks.append(Task(**payload))
                return tasks
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            print(f"配置文件 {self.config_file} 格式错误")
            return []

    async def find_by_id(self, task_id: int) -> Optional[Task]:
        """根据ID获取任务"""
        tasks = await self.find_all()
        return next((task for task in tasks if task.id == task_id), None)

    async def save(self, task: Task) -> Task:
        """保存任务（创建或更新）"""
        raise RuntimeError("JSON 任务写入已禁用，请使用 SqliteTaskRepository。")

    async def delete(self, task_id: int) -> bool:
        """删除任务"""
        raise RuntimeError("JSON 任务删除已禁用，请使用 SqliteTaskRepository。")
