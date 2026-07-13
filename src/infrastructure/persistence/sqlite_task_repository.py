"""
基于 SQLite 的任务仓储实现。
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from src.domain.models.task import Task
from src.domain.repositories.task_repository import TaskRepository
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services.task_prompt_service import TaskPromptStore


def _row_to_task(row) -> Task:
    payload = dict(row)
    payload["enabled"] = bool(payload["enabled"])
    payload["analyze_images"] = bool(payload["analyze_images"])
    payload["personal_only"] = bool(payload["personal_only"])
    payload["free_shipping"] = bool(payload["free_shipping"])
    payload["is_running"] = bool(payload["is_running"])
    payload["keyword_rules"] = json.loads(payload.pop("keyword_rules_json") or "[]")
    return Task(**payload)


def find_task_by_id_sync(task_id: int) -> Task | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    return _row_to_task(row) if row else None


def find_tasks_by_name_sync(task_name: str) -> list[Task]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE task_name = ? ORDER BY id ASC",
            (task_name,),
        ).fetchall()
    return [_row_to_task(row) for row in rows]


class SqliteTaskRepository(TaskRepository):
    """基于 SQLite 的任务仓储"""

    def __init__(
        self,
        db_path: str | None = None,
        legacy_config_file: str | None = "config.json",
    ):
        self.db_path = db_path
        self.legacy_config_file = legacy_config_file

    async def find_all(self) -> List[Task]:
        return await asyncio.to_thread(self._find_all_sync)

    async def find_by_id(self, task_id: int) -> Optional[Task]:
        return await asyncio.to_thread(self._find_by_id_sync, task_id)

    async def save(self, task: Task) -> Task:
        return await asyncio.to_thread(self._save_sync, task)

    async def delete(self, task_id: int) -> bool:
        return await asyncio.to_thread(self._delete_sync, task_id)

    def _find_all_sync(self) -> List[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
        return [_row_to_task(row) for row in rows]

    def _find_by_id_sync(self, task_id: int) -> Optional[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def _save_sync(self, task: Task) -> Task:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            task_id = task.id
            persisted_task = task
            if task_id is None:
                payload = self._task_values(task)
                payload.pop("id", None)
                cursor = conn.execute(
                    """
                    INSERT INTO tasks (
                        task_name, enabled, keyword, description, analyze_images,
                        max_pages, personal_only, min_price, max_price, cron,
                        ai_prompt_base_file, ai_prompt_criteria_file, account_state_file,
                        account_strategy, free_shipping, new_publish_option, region,
                        decision_mode, keyword_rules_json, is_running
                    ) VALUES (
                        :task_name, :enabled, :keyword, :description, :analyze_images,
                        :max_pages, :personal_only, :min_price, :max_price, :cron,
                        :ai_prompt_base_file, :ai_prompt_criteria_file, :account_state_file,
                        :account_strategy, :free_shipping, :new_publish_option, :region,
                        :decision_mode, :keyword_rules_json, :is_running
                    )
                    """,
                    payload,
                )
                task_id = int(cursor.lastrowid)
                persisted_task = task.model_copy(update={"id": task_id})
                if persisted_task.decision_mode == "ai":
                    canonical_path = TaskPromptStore().criteria_path_string(task_id)
                    conn.execute(
                        "UPDATE tasks SET ai_prompt_criteria_file = ? WHERE id = ?",
                        (canonical_path, task_id),
                    )
                    persisted_task = persisted_task.model_copy(
                        update={"ai_prompt_criteria_file": canonical_path}
                    )
            else:
                payload = self._task_values(task)
                cursor = conn.execute(
                    """
                    UPDATE tasks SET
                        task_name = :task_name,
                        enabled = :enabled,
                        keyword = :keyword,
                        description = :description,
                        analyze_images = :analyze_images,
                        max_pages = :max_pages,
                        personal_only = :personal_only,
                        min_price = :min_price,
                        max_price = :max_price,
                        cron = :cron,
                        ai_prompt_base_file = :ai_prompt_base_file,
                        ai_prompt_criteria_file = :ai_prompt_criteria_file,
                        account_state_file = :account_state_file,
                        account_strategy = :account_strategy,
                        free_shipping = :free_shipping,
                        new_publish_option = :new_publish_option,
                        region = :region,
                        decision_mode = :decision_mode,
                        keyword_rules_json = :keyword_rules_json,
                        is_running = :is_running
                    WHERE id = :id
                    """,
                    payload,
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"任务 {task_id} 不存在")
            conn.commit()
        return persisted_task.model_copy(update={"id": task_id})

    def _delete_sync(self, task_id: int) -> bool:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
        return cursor.rowcount > 0

    def _task_values(self, task: Task) -> dict:
        values = task.model_dump()
        values["enabled"] = int(task.enabled)
        values["analyze_images"] = int(task.analyze_images)
        values["personal_only"] = int(task.personal_only)
        values["free_shipping"] = int(task.free_shipping)
        values["is_running"] = int(task.is_running)
        values["keyword_rules_json"] = json.dumps(task.keyword_rules or [], ensure_ascii=False)
        values.pop("keyword_rules", None)
        return values
