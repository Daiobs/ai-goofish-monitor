import asyncio
import json
import sqlite3

import pytest

from src.domain.models.task import Task, TaskCreate
from src.infrastructure.persistence import sqlite_connection as connection_module
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import (
    SCHEMA_STATEMENTS,
    TASK_IDENTITY_MIGRATION_KEY,
    init_schema,
)
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService


def _task_create(name: str) -> TaskCreate:
    return TaskCreate(
        task_name=name,
        keyword="camera",
        description="A test task",
        decision_mode="ai",
    )


def _task(name: str) -> Task:
    return Task(**_task_create(name).model_dump(), is_running=False)


def _create_task(service: TaskService, name: str):
    return asyncio.run(service.create_task(_task_create(name)))


def _old_tasks_schema() -> str:
    return SCHEMA_STATEMENTS[1].replace(
        "CREATE TABLE IF NOT EXISTS tasks",
        "CREATE TABLE tasks",
    ).replace(" PRIMARY KEY AUTOINCREMENT", " PRIMARY KEY")


def _insert_old_task(conn: sqlite3.Connection, task_id: int, name: str) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            id, task_name, enabled, keyword, description, analyze_images,
            max_pages, personal_only, min_price, max_price, cron,
            ai_prompt_base_file, ai_prompt_criteria_file, account_state_file,
            account_strategy, free_shipping, new_publish_option, region,
            decision_mode, keyword_rules_json, is_running
        ) VALUES (?, ?, 1, 'camera', '', 1, 1, 1, NULL, NULL, NULL,
                  'prompts/base_prompt.txt', '', NULL, 'auto', 1, NULL, NULL,
                  'ai', '[]', 0)
        """,
        (task_id, name),
    )


def test_task_ids_are_monotonic_and_deleted_ids_are_not_reused(tmp_path):
    repository = SqliteTaskRepository(
        db_path=str(tmp_path / "app.sqlite3"),
        legacy_config_file=None,
    )
    service = TaskService(repository)

    first = _create_task(service, "first")
    second = _create_task(service, "second")
    assert first.id < second.id

    assert asyncio.run(service.delete_task(second.id)) is True
    third = _create_task(service, "third")

    assert third.id > second.id
    assert [task.id for task in asyncio.run(service.get_all_tasks())] == [first.id, third.id]


def test_concurrent_task_creation_uses_unique_database_ids(tmp_path):
    repository = SqliteTaskRepository(
        db_path=str(tmp_path / "app.sqlite3"),
        legacy_config_file=None,
    )

    async def create_all():
        return await asyncio.gather(
            *(repository.save(_task(f"task-{index}")) for index in range(20))
        )

    created = asyncio.run(create_all())
    task_ids = [task.id for task in created]

    assert len(task_ids) == len(set(task_ids)) == 20
    assert sorted(task_ids) == list(range(min(task_ids), max(task_ids) + 1))


def test_schema_migration_preserves_ids_and_is_idempotent(tmp_path):
    db_path = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA_STATEMENTS[0])
    conn.execute(_old_tasks_schema())
    _insert_old_task(conn, 0, "zero")
    _insert_old_task(conn, 8, "eight")
    conn.commit()

    init_schema(conn)
    init_schema(conn)

    ids = [row["id"] for row in conn.execute("SELECT id FROM tasks ORDER BY id")]
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
    ).fetchone()["sql"]
    migration = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_IDENTITY_MIGRATION_KEY,),
    ).fetchone()
    assert ids == [0, 8]
    assert "AUTOINCREMENT" in table_sql.upper()
    assert migration["value"] == "done"

    cursor = conn.execute(
        """
        INSERT INTO tasks (
            task_name, enabled, keyword, description, analyze_images, max_pages,
            personal_only, min_price, max_price, cron, ai_prompt_base_file,
            ai_prompt_criteria_file, account_state_file, account_strategy,
            free_shipping, new_publish_option, region, decision_mode,
            keyword_rules_json, is_running
        ) VALUES ('next', 1, 'camera', '', 1, 1, 1, NULL, NULL, NULL,
                  'prompts/base_prompt.txt', '', NULL, 'auto', 1, NULL, NULL,
                  'ai', '[]', 0)
        """
    )
    assert cursor.lastrowid == 9
    conn.close()


def test_schema_migration_failure_rolls_back_original_table(tmp_path, monkeypatch):
    db_path = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA_STATEMENTS[0])
    conn.execute(_old_tasks_schema())
    _insert_old_task(conn, 4, "preserved")
    conn.commit()

    def fail_copy(_conn):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(connection_module, "_copy_tasks_to_autoincrement_table", fail_copy)

    with pytest.raises(RuntimeError, match="copy failed"):
        init_schema(conn)

    row = conn.execute("SELECT id, task_name FROM tasks").fetchone()
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
    ).fetchone()["sql"]
    marker = conn.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (TASK_IDENTITY_MIGRATION_KEY,),
    ).fetchone()
    temp_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks__task_id_migration'"
    ).fetchone()
    assert (row["id"], row["task_name"]) == (4, "preserved")
    assert "AUTOINCREMENT" not in table_sql.upper()
    assert marker is None
    assert temp_table is None
    conn.close()


def test_legacy_config_preserves_explicit_ids_and_advances_sequence(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    **_task_create("explicit").model_dump(),
                    "id": 12,
                },
                _task_create("implicit").model_dump(),
            ]
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "app.sqlite3"
    bootstrap_sqlite_storage(
        str(db_path),
        legacy_config_file=str(config_path),
        legacy_result_dir=str(tmp_path / "missing-results"),
        legacy_price_history_dir=str(tmp_path / "missing-snapshots"),
    )
    repository = SqliteTaskRepository(
        db_path=str(db_path),
        legacy_config_file=str(config_path),
    )
    service = TaskService(repository)

    assert [task.id for task in asyncio.run(service.get_all_tasks())] == [0, 12]
    created = _create_task(service, "after-import")
    assert created.id == 13
