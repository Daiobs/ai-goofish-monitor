import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from src.domain.models.task import Task, TaskCreate, TaskGenerateRequest, TaskUpdate
from src.infrastructure.persistence.sqlite_bootstrap import (
    TASK_PROMPT_MIGRATION_PREFIX,
    bootstrap_sqlite_storage,
    migrate_task_prompts,
)
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services import task_prompt_service as prompt_module
from src.services.task_generation_runner import run_ai_generation_job
from src.services.task_generation_service import TaskGenerationService
from src.services.task_prompt_service import TaskPromptStore
from src.services.task_service import TaskPromptIntegrityError, TaskService


def _task_create(name="same-name", keyword="same-keyword") -> TaskCreate:
    return TaskCreate(
        task_name=name,
        keyword=keyword,
        description="original description",
        decision_mode="ai",
    )


def test_same_name_and_keyword_tasks_have_isolated_prompt_lifecycles(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "base_prompt.txt").write_text(
        "shared base",
        encoding="utf-8",
    )
    repository = SqliteTaskRepository(
        db_path=str(tmp_path / "app.sqlite3"),
        legacy_config_file=None,
    )
    service = TaskService(repository)

    first = asyncio.run(
        service.create_ai_task_with_criteria(_task_create(), "first criteria")
    )
    second = asyncio.run(
        service.create_ai_task_with_criteria(_task_create(), "second criteria")
    )

    assert first.id != second.id
    assert first.ai_prompt_criteria_file == f"prompts/tasks/{first.id}/criteria.txt"
    assert second.ai_prompt_criteria_file == f"prompts/tasks/{second.id}/criteria.txt"
    assert first.ai_prompt_criteria_file != second.ai_prompt_criteria_file
    second_path = tmp_path / second.ai_prompt_criteria_file

    updated = asyncio.run(
        service.update_task_with_generated_criteria(
            first.id,
            TaskUpdate(
                task_name="renamed-task",
                keyword="renamed-keyword",
                description="updated description",
            ),
            "updated first criteria",
        )
    )

    assert updated.id == first.id
    assert updated.ai_prompt_criteria_file == first.ai_prompt_criteria_file
    assert (tmp_path / updated.ai_prompt_criteria_file).read_text(
        encoding="utf-8"
    ) == "updated first criteria"
    assert second_path.read_text(encoding="utf-8") == "second criteria"

    assert asyncio.run(service.delete_task(first.id)) is True
    assert not (tmp_path / "prompts" / "tasks" / str(first.id)).exists()
    assert second_path.read_text(encoding="utf-8") == "second criteria"
    assert (tmp_path / "prompts" / "base_prompt.txt").read_text(
        encoding="utf-8"
    ) == "shared base"


def test_direct_create_copies_only_prompt_root_legacy_source(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    source = prompts_dir / "legacy.txt"
    source.write_text("legacy criteria", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    repository = SqliteTaskRepository(
        db_path=str(tmp_path / "app.sqlite3"),
        legacy_config_file=None,
    )
    service = TaskService(repository)

    compatible_payload = _task_create().model_copy(
        update={"ai_prompt_criteria_file": "prompts/legacy.txt"}
    )
    compatible = asyncio.run(service.create_task(compatible_payload))
    outside_payload = _task_create(name="outside").model_copy(
        update={"ai_prompt_criteria_file": str(outside)}
    )
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path.resolve() == outside.resolve():
            raise AssertionError("outside source must not be read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    with pytest.raises(TaskPromptIntegrityError, match="criteria"):
        asyncio.run(service.create_task(outside_payload))

    assert TaskPromptStore().read_criteria(compatible.id) == "legacy criteria"
    assert [task.id for task in asyncio.run(service.get_all_tasks())] == [compatible.id]
    assert not (tmp_path / "prompts" / "tasks" / "2").exists()
    assert original_read_text(outside, encoding="utf-8") == "outside secret"


@pytest.mark.parametrize("source_kind", ["missing", "empty", "unreadable", "copy"])
def test_direct_ai_creation_rolls_back_invalid_criteria_sources(
    tmp_path,
    monkeypatch,
    source_kind,
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    source = prompts_dir / f"{source_kind}.txt"
    if source_kind != "missing":
        source.write_text(
            "" if source_kind == "empty" else "fictional criteria",
            encoding="utf-8",
        )

    original_read_text = Path.read_text
    if source_kind == "unreadable":
        def fail_source_read(path, *args, **kwargs):
            if path.resolve() == source.resolve():
                raise PermissionError("fictional unreadable source")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_source_read)
    elif source_kind == "copy":
        def fail_replace(_source, _target):
            raise OSError("fictional copy failure")

        monkeypatch.setattr(prompt_module.os, "replace", fail_replace)

    repository = SqliteTaskRepository(
        db_path=str(tmp_path / "app.sqlite3"),
        legacy_config_file=None,
    )
    service = TaskService(repository)
    payload = _task_create().model_copy(
        update={"ai_prompt_criteria_file": source.relative_to(tmp_path).as_posix()}
    )

    with pytest.raises(TaskPromptIntegrityError, match="criteria"):
        asyncio.run(service.create_task(payload))

    assert asyncio.run(service.get_all_tasks()) == []
    assert not (prompts_dir / "tasks").exists()


def test_failed_database_compensation_still_removes_prompt_and_preserves_error(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.chdir(tmp_path)
    store = TaskPromptStore()
    stale_path = store.criteria_path(41)
    stale_path.parent.mkdir(parents=True)
    (stale_path.parent / "stale.tmp").write_text("fictional", encoding="utf-8")
    payload = _task_create().model_dump()
    payload.update(
        {
            "id": 41,
            "ai_prompt_criteria_file": store.criteria_path_string(41),
            "is_running": False,
        }
    )
    persisted = Task(**payload)

    class FailingRepository:
        async def save(self, _task):
            return persisted

        async def delete(self, _task_id):
            raise RuntimeError("secondary cleanup secret")

    def fail_write(_task_id, _content):
        raise ValueError("original criteria write failure")

    monkeypatch.setattr(store, "write_criteria", fail_write)
    service = TaskService(FailingRepository(), prompt_store=store)

    with pytest.raises(ValueError, match="original criteria write failure"):
        asyncio.run(
            service.create_ai_task_with_criteria(
                _task_create(),
                "generated criteria",
            )
        )

    output = capsys.readouterr().out
    assert "secondary cleanup secret" not in output
    assert not stale_path.parent.exists()


def test_atomic_prompt_write_failure_leaves_no_partial_file(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    store = TaskPromptStore()

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(prompt_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.write_criteria(17, "new criteria")

    task_dir = tmp_path / "prompts" / "tasks" / "17"
    assert not (task_dir / "criteria.txt").exists()
    assert list(task_dir.glob("*.tmp")) == []
    assert not task_dir.exists()


def test_database_update_failure_restores_previous_prompt(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    store = TaskPromptStore()
    existing_payload = _task_create().model_dump()
    existing_payload.update(
        {
            "id": 23,
            "ai_prompt_criteria_file": store.criteria_path_string(23),
            "is_running": False,
        }
    )
    existing = Task(**existing_payload)
    store.write_criteria(23, "previous criteria")

    class FailingRepository:
        async def find_by_id(self, task_id):
            return existing if task_id == existing.id else None

        async def save(self, _task):
            raise RuntimeError("database update failed")

        async def delete(self, _task_id):
            return False

        async def find_all(self):
            return [existing]

    service = TaskService(FailingRepository(), prompt_store=store)

    with pytest.raises(RuntimeError, match="database update failed"):
        asyncio.run(
            service.update_task_with_generated_criteria(
                23,
                TaskUpdate(description="updated description"),
                "replacement criteria",
            )
        )

    assert store.read_criteria(23) == "previous criteria"


def test_task_creation_database_failure_compensates_created_record(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    class FailingRepository:
        def __init__(self):
            self.save_calls = 0
            self.deleted = []

        async def save(self, task):
            self.save_calls += 1
            if self.save_calls == 1:
                return task.model_copy(update={"id": 29})
            raise RuntimeError("canonical path update failed")

        async def delete(self, task_id):
            self.deleted.append(task_id)
            return True

        async def find_by_id(self, _task_id):
            return None

        async def find_all(self):
            return []

    repository = FailingRepository()
    service = TaskService(repository)

    with pytest.raises(RuntimeError, match="canonical path update failed"):
        asyncio.run(service.create_task(_task_create()))

    assert repository.deleted == [29]
    assert not (tmp_path / "prompts" / "tasks" / "29").exists()


def test_generation_failure_after_persist_removes_task_and_prompt(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    repository = SqliteTaskRepository(
        db_path=str(tmp_path / "app.sqlite3"),
        legacy_config_file=None,
    )
    task_service = TaskService(repository)
    generation_service = TaskGenerationService()
    job = asyncio.run(generation_service.create_job("generated-task"))
    request = TaskGenerateRequest(
        task_name="generated-task",
        keyword="camera",
        description="generated description",
    )

    async def fake_generate_criteria(*_args, **_kwargs):
        return "generated criteria"

    class FailingScheduler:
        async def reload_jobs(self, _tasks):
            raise RuntimeError("scheduler reload failed")

    monkeypatch.setattr(
        "src.services.task_generation_runner.generate_criteria",
        fake_generate_criteria,
    )

    asyncio.run(
        run_ai_generation_job(
            job_id=job.job_id,
            req=request,
            task_service=task_service,
            scheduler_service=FailingScheduler(),
            generation_service=generation_service,
        )
    )

    latest = asyncio.run(generation_service.get_job(job.job_id))
    assert latest.status == "failed"
    assert asyncio.run(task_service.get_all_tasks()) == []
    assert not (tmp_path / "prompts" / "tasks").exists()


def test_legacy_prompt_migration_is_isolated_idempotent_and_retryable(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    shared_path = prompts_dir / "shared.txt"
    shared_path.write_text("shared legacy criteria", encoding="utf-8")
    interrupted_source = prompts_dir / "interrupted.txt"
    interrupted_source.write_text("old interrupted content", encoding="utf-8")
    missing_path = prompts_dir / "missing-secret-source.txt"

    raw_tasks = []
    for task_id, source in (
        (5, shared_path),
        (8, shared_path),
        (9, missing_path),
        (10, interrupted_source),
    ):
        payload = _task_create(name=f"task-{task_id}").model_dump()
        payload.update(
            {
                "id": task_id,
                "ai_prompt_criteria_file": source.relative_to(tmp_path).as_posix(),
            }
        )
        raw_tasks.append(payload)

    config_path = tmp_path / "legacy-config.json"
    config_path.write_text(json.dumps(raw_tasks), encoding="utf-8")
    db_path = tmp_path / "app.sqlite3"
    bootstrap_sqlite_storage(
        str(db_path),
        legacy_config_file=str(config_path),
        legacy_result_dir=str(tmp_path / "no-results"),
        legacy_price_history_dir=str(tmp_path / "no-snapshots"),
    )

    store = TaskPromptStore()
    store.write_criteria(10, "already recovered target")
    first = migrate_task_prompts(str(db_path), prompt_store=store)
    first_output = capsys.readouterr().out

    assert first == {"migrated": 3, "missing": 1, "failed": 0}
    assert "missing-secret-source" not in first_output
    assert store.read_criteria(5) == "shared legacy criteria"
    assert store.read_criteria(8) == "shared legacy criteria"
    assert store.read_criteria(10) == "already recovered target"
    assert shared_path.read_text(encoding="utf-8") == "shared legacy criteria"
    assert interrupted_source.exists()

    store.write_criteria(5, "independently updated")
    second = migrate_task_prompts(str(db_path), prompt_store=store)
    assert second == {"migrated": 0, "missing": 1, "failed": 0}
    assert store.read_criteria(5) == "independently updated"
    assert store.read_criteria(8) == "shared legacy criteria"

    missing_path.write_text("restored missing criteria", encoding="utf-8")
    third = migrate_task_prompts(str(db_path), prompt_store=store)
    assert third == {"migrated": 1, "missing": 0, "failed": 0}
    assert store.read_criteria(9) == "restored missing criteria"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ai_prompt_criteria_file FROM tasks ORDER BY id"
    ).fetchall()
    markers = conn.execute(
        "SELECT key FROM app_metadata WHERE key LIKE ? ORDER BY key",
        (f"{TASK_PROMPT_MIGRATION_PREFIX}%",),
    ).fetchall()
    conn.close()

    assert [row["ai_prompt_criteria_file"] for row in rows] == [
        f"prompts/tasks/{task_id}/criteria.txt" for task_id in (5, 8, 9, 10)
    ]
    assert len(markers) == 4


@pytest.mark.parametrize("source_kind", ["absolute", "traversal", "symlink"])
def test_prompt_migration_rejects_sources_outside_prompt_root(
    tmp_path,
    monkeypatch,
    capsys,
    source_kind,
):
    monkeypatch.chdir(tmp_path)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    outside = tmp_path / "outside-fictional.txt"
    outside.write_text("fictional external criteria", encoding="utf-8")

    if source_kind == "absolute":
        source_value = str(outside)
    elif source_kind == "traversal":
        source_value = "prompts/../outside-fictional.txt"
    else:
        link = prompts_dir / "outside-link.txt"
        link.symlink_to(outside)
        source_value = "prompts/outside-link.txt"

    payload = _task_create(name=f"unsafe-{source_kind}").model_dump()
    payload.update({"id": 31, "ai_prompt_criteria_file": source_value})
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps([payload]), encoding="utf-8")
    db_path = tmp_path / "app.sqlite3"
    bootstrap_sqlite_storage(
        str(db_path),
        legacy_config_file=str(config_path),
        legacy_result_dir=str(tmp_path / "no-results"),
        legacy_price_history_dir=str(tmp_path / "no-snapshots"),
    )

    result = migrate_task_prompts(str(db_path), prompt_store=TaskPromptStore())
    output = capsys.readouterr().out

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT ai_prompt_criteria_file FROM tasks WHERE id = 31"
    ).fetchone()
    marker = conn.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (f"{TASK_PROMPT_MIGRATION_PREFIX}31",),
    ).fetchone()
    conn.close()

    assert result == {"migrated": 0, "missing": 1, "failed": 0}
    assert row["ai_prompt_criteria_file"] == source_value
    assert marker is None
    assert TaskPromptStore().read_criteria(31) is None
    assert "fictional external criteria" not in output
    assert source_value not in output
    assert outside.read_text(encoding="utf-8") == "fictional external criteria"
