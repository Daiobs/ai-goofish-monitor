import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import dependencies as deps
from src.api.routes import results
from src.domain.models.task import Task
from src.infrastructure.persistence.storage_names import (
    build_legacy_result_filename,
    build_task_result_filename,
)
from src.services.dashboard_service import build_dashboard_snapshot
from src.services.price_history_service import record_market_snapshots
from src.services.result_storage_service import (
    list_result_filenames,
    save_result_record,
    save_task_result_record,
)


TASK_ID = 42
COLLIDING_KEYWORD = "task_42"


class FakeTaskService:
    async def get_task(self, task_id: int):
        if task_id != TASK_ID:
            return None
        return SimpleNamespace(
            id=TASK_ID,
            task_name="Canonical task",
            keyword=COLLIDING_KEYWORD,
        )


def _record(*, title: str, task_name: str) -> dict:
    return {
        "爬取时间": "2026-07-14T10:00:00",
        "搜索关键字": COLLIDING_KEYWORD,
        "任务名称": task_name,
        "商品信息": {
            "商品ID": "shared",
            "商品标题": title,
            "商品链接": "https://www.goofish.com/item?id=shared",
            "当前售价": "320",
        },
        "ai_analysis": {
            "analysis_source": "keyword",
            "is_recommended": True,
            "keyword_hit_count": 1,
        },
    }


def _task() -> Task:
    return Task(
        id=TASK_ID,
        task_name="Canonical task",
        enabled=True,
        keyword=COLLIDING_KEYWORD,
        max_pages=1,
        personal_only=True,
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/task_42.txt",
    )


@pytest.fixture()
def collision_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    app = FastAPI()
    app.include_router(results.router)
    app.dependency_overrides[deps.get_task_service] = lambda: FakeTaskService()
    with TestClient(app) as client:
        yield client


def _seed_collision() -> tuple[str, str]:
    canonical = build_task_result_filename(TASK_ID)
    escaped = build_legacy_result_filename(COLLIDING_KEYWORD)
    assert canonical != escaped

    assert asyncio.run(
        save_task_result_record(
            _record(title="Task-owned item", task_name="Canonical task"),
            COLLIDING_KEYWORD,
            TASK_ID,
        )
    )
    assert asyncio.run(
        save_result_record(
            _record(title="Escaped legacy item", task_name="Legacy task"),
            COLLIDING_KEYWORD,
        )
    )
    record_market_snapshots(
        task_id=TASK_ID,
        keyword=COLLIDING_KEYWORD,
        task_name="Canonical task",
        items=[{"商品ID": "shared", "商品标题": "Task-owned item", "当前售价": "100"}],
        run_id="task-run",
        snapshot_time="2026-07-14T10:00:00",
    )
    record_market_snapshots(
        keyword=COLLIDING_KEYWORD,
        task_name="Legacy task",
        items=[{"商品ID": "shared", "商品标题": "Escaped legacy item", "当前售价": "320"}],
        run_id="legacy-run",
        snapshot_time="2026-07-14T10:00:00",
    )
    return canonical, escaped


def test_colliding_task_and_legacy_filename_apis_remain_independent(collision_client):
    canonical, escaped = _seed_collision()

    assert set(asyncio.run(list_result_filenames())) == {canonical, escaped}
    assert set(collision_client.get("/api/results/files").json()["files"]) == {
        canonical,
        escaped,
    }

    task_query = collision_client.get(f"/api/results/tasks/{TASK_ID}")
    canonical_query = collision_client.get(f"/api/results/{canonical}")
    legacy_query = collision_client.get(f"/api/results/{escaped}")
    assert task_query.json()["items"][0]["商品信息"]["商品标题"] == "Task-owned item"
    assert canonical_query.json()["items"][0]["商品信息"]["商品标题"] == "Task-owned item"
    assert legacy_query.json()["items"][0]["商品信息"]["商品标题"] == "Escaped legacy item"
    assert legacy_query.json()["items"][0]["price_insight"]["market_avg_price"] == 320.0

    legacy_insights = collision_client.get(f"/api/results/{escaped}/insights")
    assert legacy_insights.status_code == 200
    assert legacy_insights.json()["market_summary"]["avg_price"] == 320.0

    task_download = collision_client.get(f"/api/results/files/{canonical}")
    legacy_download = collision_client.get(f"/api/results/files/{escaped}")
    assert "Task-owned item" in task_download.text
    assert "Escaped legacy item" not in task_download.text
    assert "Escaped legacy item" in legacy_download.text
    assert "Task-owned item" not in legacy_download.text

    task_export = collision_client.get(f"/api/results/{canonical}/export")
    legacy_export = collision_client.get(f"/api/results/{escaped}/export")
    assert "Task-owned item" in task_export.text
    assert "Escaped legacy item" not in task_export.text
    assert "Escaped legacy item" in legacy_export.text
    assert "Task-owned item" not in legacy_export.text

    task_rules = collision_client.put(
        f"/api/results/tasks/{TASK_ID}/blacklist-rules",
        json={"keywords": ["task-only-rule"]},
    )
    legacy_rules = collision_client.put(
        f"/api/results/{escaped}/blacklist-rules",
        json={"keywords": ["legacy-only-rule"]},
    )
    assert task_rules.status_code == 200
    assert legacy_rules.status_code == 200
    assert collision_client.get(
        f"/api/results/{canonical}/blacklist-rules"
    ).json()["keywords"] == ["task-only-rule"]
    assert collision_client.get(
        f"/api/results/{escaped}/blacklist-rules"
    ).json()["keywords"] == ["legacy-only-rule"]

    hidden = collision_client.patch(
        f"/api/results/{escaped}/items/shared/status",
        json={"status": "hidden"},
    )
    assert hidden.status_code == 200
    assert collision_client.get(f"/api/results/{escaped}").json()["total_items"] == 0
    assert collision_client.get(f"/api/results/tasks/{TASK_ID}").json()["total_items"] == 1


def test_filename_deletes_preserve_the_other_collision_owner(collision_client):
    canonical, escaped = _seed_collision()

    deleted_legacy = collision_client.delete(f"/api/results/files/{escaped}")
    assert deleted_legacy.status_code == 200
    assert collision_client.get(f"/api/results/{escaped}").status_code == 404
    assert collision_client.get(f"/api/results/{canonical}").json()["total_items"] == 1

    assert asyncio.run(
        save_result_record(
            _record(title="Escaped legacy item", task_name="Legacy task"),
            COLLIDING_KEYWORD,
        )
    )
    deleted_task_file = collision_client.delete(f"/api/results/files/{canonical}")
    assert deleted_task_file.status_code == 200
    assert collision_client.get(f"/api/results/{canonical}").status_code == 404
    assert collision_client.get(f"/api/results/{escaped}").json()["total_items"] == 1


def test_task_scoped_delete_preserves_colliding_legacy_results(collision_client):
    _, escaped = _seed_collision()

    deleted = collision_client.delete(f"/api/results/tasks/{TASK_ID}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted_results"] == 1
    legacy_query = collision_client.get(f"/api/results/{escaped}")
    assert legacy_query.status_code == 200
    assert legacy_query.json()["items"][0]["商品信息"]["商品标题"] == "Escaped legacy item"


@pytest.mark.parametrize(
    ("include_task", "include_legacy"),
    ((False, True), (True, False), (True, True)),
    ids=("only-legacy", "only-task", "both"),
)
def test_result_listing_keeps_collision_owners_distinct(
    collision_client,
    include_task,
    include_legacy,
):
    canonical = build_task_result_filename(TASK_ID)
    escaped = build_legacy_result_filename(COLLIDING_KEYWORD)
    if include_task:
        assert asyncio.run(
            save_task_result_record(
                _record(title="Task-owned item", task_name="Canonical task"),
                COLLIDING_KEYWORD,
                TASK_ID,
            )
        )
    if include_legacy:
        assert asyncio.run(
            save_result_record(
                _record(title="Escaped legacy item", task_name="Legacy task"),
                COLLIDING_KEYWORD,
            )
        )

    expected = {
        filename
        for filename, included in (
            (canonical, include_task),
            (escaped, include_legacy),
        )
        if included
    }
    assert set(collision_client.get("/api/results/files").json()["files"]) == expected


@pytest.mark.parametrize(
    ("include_task", "include_legacy"),
    ((False, True), (True, False), (True, True)),
    ids=("only-legacy", "only-task", "both"),
)
def test_dashboard_does_not_map_escaped_task_42_legacy_to_task(
    collision_client,
    include_task,
    include_legacy,
):
    canonical = build_task_result_filename(TASK_ID)
    escaped = build_legacy_result_filename(COLLIDING_KEYWORD)
    if include_task:
        asyncio.run(
            save_task_result_record(
                _record(title="Task-owned item", task_name="Canonical task"),
                COLLIDING_KEYWORD,
                TASK_ID,
            )
        )
    if include_legacy:
        asyncio.run(
            save_result_record(
                _record(title="Escaped legacy item", task_name="Legacy task"),
                COLLIDING_KEYWORD,
            )
        )

    snapshot = asyncio.run(build_dashboard_snapshot([_task()]))
    summaries_by_filename = {
        item["filename"]: item
        for item in snapshot["task_summaries"]
        if item["filename"] is not None
    }
    expected_filenames = {
        filename
        for filename, included in (
            (canonical, include_task),
            (escaped, include_legacy),
        )
        if included
    }
    assert set(summaries_by_filename) == expected_filenames
    if include_task:
        assert summaries_by_filename[canonical]["task_id"] == TASK_ID
    if include_legacy:
        assert summaries_by_filename[escaped]["task_id"] is None
        assert summaries_by_filename[escaped]["keyword"] == COLLIDING_KEYWORD

    result_activity_filenames = {
        item["filename"]
        for item in snapshot["recent_activities"]
        if item["filename"] is not None
    }
    assert result_activity_filenames == expected_filenames
