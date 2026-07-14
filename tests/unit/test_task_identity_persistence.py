import asyncio
import json
import sqlite3

import pytest

from src.domain.models.task import Task, TaskCreate
from src.infrastructure.persistence import sqlite_connection as connection_module
from src.infrastructure.persistence.json_task_repository import JsonTaskRepository
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import (
    RESULT_STATUS_MIGRATION_KEY,
    SCHEMA_STATEMENTS,
    TASK_IDENTITY_MIGRATION_KEY,
    init_schema,
)
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService


SCHEMA_WRITE_PREFIXES = (
    "BEGIN IMMEDIATE",
    "CREATE ",
    "DROP ",
    "ALTER ",
    "INSERT ",
    "DELETE ",
    "UPDATE ",
)


def _task_create(name: str) -> TaskCreate:
    return TaskCreate(
        task_name=name,
        keyword="camera",
        description="",
        decision_mode="keyword",
        keyword_rules=["camera"],
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


def _result_status_columns(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(result_items)").fetchall()
    }


def _result_status_index_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_results_filename_status_crawl'"
        ).fetchone()
        is not None
    )


def _assert_schema_trace_is_read_only(statements: list[str]) -> None:
    normalized = [statement.strip().upper() for statement in statements]
    assert not any(
        statement.startswith(SCHEMA_WRITE_PREFIXES) for statement in normalized
    )


def _insert_result_item(conn: sqlite3.Connection, item_id: str) -> None:
    conn.execute(
        """
        INSERT INTO result_items (
            result_filename, keyword, task_name, crawl_time, item_id,
            link_unique_key, is_recommended, keyword_hit_count, raw_json
        ) VALUES ('camera.jsonl', 'camera', 'task', '2026-01-01T00:00:00',
                  ?, ?, 0, 0, '{}')
        """,
        (item_id, f"item:{item_id}"),
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


def test_reinitialization_never_reuses_deleted_historical_sequence(tmp_path):
    db_path = tmp_path / "app.sqlite3"
    repository = SqliteTaskRepository(db_path=str(db_path), legacy_config_file=None)
    service = TaskService(repository)
    created = [_create_task(service, f"task-{index}") for index in range(20)]
    assert created[-1].id == 20
    assert asyncio.run(service.delete_task(20)) is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "DELETE FROM app_metadata WHERE key = ?",
        (TASK_IDENTITY_MIGRATION_KEY,),
    )
    conn.commit()
    init_schema(conn)
    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'tasks'"
    ).fetchone()["seq"]
    conn.close()

    assert sequence == 20
    assert _create_task(service, "after-reinit").id > 20


def test_empty_tasks_table_preserves_historical_sequence(tmp_path):
    db_path = tmp_path / "app.sqlite3"
    repository = SqliteTaskRepository(db_path=str(db_path), legacy_config_file=None)
    service = TaskService(repository)
    asyncio.run(service.get_all_tasks())

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("DELETE FROM tasks")
    conn.execute(
        "INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('tasks', 50)"
    )
    conn.execute(
        "DELETE FROM app_metadata WHERE key = ?",
        (TASK_IDENTITY_MIGRATION_KEY,),
    )
    conn.commit()
    init_schema(conn)
    init_schema(conn)
    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'tasks'"
    ).fetchone()["seq"]
    conn.close()

    assert sequence == 50
    assert _create_task(service, "after-empty-table").id == 51


def test_repeated_task_migration_never_lowers_sequence(tmp_path):
    db_path = tmp_path / "app.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('tasks', 75)"
    )
    conn.commit()

    for _ in range(2):
        conn.execute(
            "DELETE FROM app_metadata WHERE key = ?",
            (TASK_IDENTITY_MIGRATION_KEY,),
        )
        conn.commit()
        init_schema(conn)

    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'tasks'"
    ).fetchone()["seq"]
    conn.close()

    assert sequence == 75


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


def test_current_schema_init_is_read_only_and_avoids_immediate_lock(tmp_path):
    conn = sqlite3.connect(tmp_path / "app.sqlite3")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    statements = []
    conn.set_trace_callback(statements.append)

    init_schema(conn)
    conn.execute("SELECT * FROM tasks").fetchall()

    _assert_schema_trace_is_read_only(statements)
    conn.close()


def test_result_status_missing_index_is_repaired_once(tmp_path):
    conn = sqlite3.connect(tmp_path / "app.sqlite3")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute("DROP INDEX idx_results_filename_status_crawl")
    conn.commit()
    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (RESULT_STATUS_MIGRATION_KEY,),
    ).fetchone()
    assert marker["value"] == "done"

    first_statements = []
    conn.set_trace_callback(first_statements.append)
    init_schema(conn)

    assert _result_status_index_exists(conn) is True
    assert any(
        statement.strip().upper().startswith("BEGIN IMMEDIATE")
        for statement in first_statements
    )
    assert any(
        "CREATE INDEX IF NOT EXISTS IDX_RESULTS_FILENAME_STATUS_CRAWL"
        in statement.upper()
        for statement in first_statements
    )

    second_statements = []
    conn.set_trace_callback(second_statements.append)
    init_schema(conn)
    _assert_schema_trace_is_read_only(second_statements)
    conn.close()


def test_result_status_missing_column_preserves_data_and_repairs_once(tmp_path):
    conn = sqlite3.connect(tmp_path / "app.sqlite3")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    _insert_result_item(conn, "preserved-item")
    conn.execute("DROP INDEX idx_results_filename_status_crawl")
    conn.execute("ALTER TABLE result_items DROP COLUMN status")
    conn.commit()
    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (RESULT_STATUS_MIGRATION_KEY,),
    ).fetchone()
    assert marker["value"] == "done"

    first_statements = []
    conn.set_trace_callback(first_statements.append)
    init_schema(conn)

    preserved = conn.execute(
        "SELECT item_id, status FROM result_items WHERE item_id = 'preserved-item'"
    ).fetchone()
    assert _result_status_columns(conn) >= {"status"}
    assert _result_status_index_exists(conn) is True
    assert any(
        statement.strip().upper().startswith("ALTER TABLE RESULT_ITEMS")
        for statement in first_statements
    )
    assert (preserved["item_id"], preserved["status"]) == (
        "preserved-item",
        "active",
    )

    second_statements = []
    conn.set_trace_callback(second_statements.append)
    init_schema(conn)
    _assert_schema_trace_is_read_only(second_statements)
    conn.close()


def test_result_status_missing_marker_only_writes_marker(tmp_path):
    conn = sqlite3.connect(tmp_path / "app.sqlite3")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        "DELETE FROM app_metadata WHERE key = ?",
        (RESULT_STATUS_MIGRATION_KEY,),
    )
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)

    init_schema(conn)

    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (RESULT_STATUS_MIGRATION_KEY,),
    ).fetchone()
    normalized = [statement.strip().upper() for statement in statements]
    structural_prefixes = ("CREATE ", "DROP ", "ALTER ", "UPDATE ", "DELETE ")
    assert marker["value"] == "done"
    assert not any(
        statement.startswith(structural_prefixes) for statement in normalized
    )
    assert sum(
        statement.startswith("INSERT OR REPLACE INTO APP_METADATA")
        for statement in normalized
    ) == 1
    conn.close()


def test_result_status_repair_failure_rolls_back_structure_and_marker(
    tmp_path,
    monkeypatch,
):
    conn = sqlite3.connect(tmp_path / "app.sqlite3")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    _insert_result_item(conn, "rollback-item")
    conn.execute("DROP INDEX idx_results_filename_status_crawl")
    conn.execute("ALTER TABLE result_items DROP COLUMN status")
    conn.execute(
        "DELETE FROM app_metadata WHERE key = ?",
        (RESULT_STATUS_MIGRATION_KEY,),
    )
    conn.commit()

    def fail_marker(_conn):
        raise RuntimeError("marker write failed")

    monkeypatch.setattr(
        connection_module,
        "_write_result_status_migration_marker",
        fail_marker,
    )

    with pytest.raises(RuntimeError, match="marker write failed"):
        init_schema(conn)

    marker = conn.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (RESULT_STATUS_MIGRATION_KEY,),
    ).fetchone()
    preserved = conn.execute(
        "SELECT item_id FROM result_items WHERE item_id = 'rollback-item'"
    ).fetchone()
    assert "status" not in _result_status_columns(conn)
    assert _result_status_index_exists(conn) is False
    assert marker is None
    assert preserved["item_id"] == "rollback-item"
    conn.close()


def test_old_schema_enters_immediate_atomic_migration(tmp_path):
    conn = sqlite3.connect(tmp_path / "app.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA_STATEMENTS[0])
    conn.execute(_old_tasks_schema())
    _insert_old_task(conn, 4, "preserved")
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)

    init_schema(conn)

    assert any(
        statement.strip().upper().startswith("BEGIN IMMEDIATE")
        for statement in statements
    )
    conn.close()


def test_multiple_database_paths_initialize_independently(tmp_path):
    for name in ("first.sqlite3", "second.sqlite3"):
        conn = sqlite3.connect(tmp_path / name)
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        markers = {
            row["key"]
            for row in conn.execute(
                "SELECT key FROM app_metadata WHERE key IN (?, ?)",
                (TASK_IDENTITY_MIGRATION_KEY, RESULT_STATUS_MIGRATION_KEY),
            ).fetchall()
        }
        assert markers == {
            TASK_IDENTITY_MIGRATION_KEY,
            RESULT_STATUS_MIGRATION_KEY,
        }
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


def test_legacy_json_repository_is_read_only_and_never_reindexes(tmp_path):
    config_path = tmp_path / "config.json"
    first = _task_create("first").model_dump()
    first["id"] = 0
    second = _task_create("second").model_dump()
    second["id"] = 9
    config_path.write_text(json.dumps([first, second]), encoding="utf-8")
    repository = JsonTaskRepository(str(config_path))

    assert asyncio.run(repository.find_by_id(9)).task_name == "second"
    assert asyncio.run(repository.find_by_id(1)) is None
    assert asyncio.run(repository.find_by_id(0)).task_name == "first"
    with pytest.raises(RuntimeError, match="JSON 任务删除已禁用"):
        asyncio.run(repository.delete(0))
    with pytest.raises(RuntimeError, match="JSON 任务写入已禁用"):
        asyncio.run(repository.save(_task("new")))
    assert json.loads(config_path.read_text(encoding="utf-8")) == [first, second]
