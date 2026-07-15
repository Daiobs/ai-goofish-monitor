import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import dependencies as deps
from src.api.routes import dashboard
from src.domain.models.task import TaskCreate
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import build_task_result_filename
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.result_storage_service import save_task_result_record
from src.services.task_service import TaskService


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_dashboard_summary_aggregates_tasks_and_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)

    repository = SqliteTaskRepository(
        db_path=str(tmp_path / "app.sqlite3"),
        legacy_config_file=None,
    )
    task_service = TaskService(repository)
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[deps.get_task_service] = lambda: task_service

    client = TestClient(app)

    first = TaskCreate(
      task_name="Apple Watch 任务",
      keyword="apple watch",
      description="只关注价格合适且成色好的 Apple Watch。",
      max_pages=3,
      personal_only=True,
    )
    second = TaskCreate(
      task_name="iPad 任务",
      keyword="ipad pro",
      description="关注 2024 款 iPad Pro。",
      max_pages=2,
      personal_only=True,
    )

    created_first = asyncio.run(
        task_service.create_ai_task_with_criteria(first, "fictional watch criteria")
    )
    created_second = asyncio.run(
        task_service.create_ai_task_with_criteria(second, "fictional iPad criteria")
    )
    asyncio.run(task_service.update_task_status(created_second.id, True))

    records = [
        {
            "爬取时间": "2026-03-10T10:00:00",
            "搜索关键字": "apple watch",
            "任务名称": "Apple Watch 任务",
            "商品信息": {
                "商品ID": "watch-1",
                "商品标题": "Apple Watch S10",
                "商品链接": "https://www.goofish.com/item?id=watch-1",
                "当前售价": "¥1800",
            },
            "ai_analysis": {
                "analysis_source": "ai",
                "is_recommended": True,
                "reason": "价格低于均价",
            },
        },
        {
            "爬取时间": "2026-03-10T11:00:00",
            "搜索关键字": "apple watch",
            "任务名称": "Apple Watch 任务",
            "商品信息": {
                "商品ID": "watch-2",
                "商品标题": "Apple Watch S10 蜂窝版",
                "商品链接": "https://www.goofish.com/item?id=watch-2",
                "当前售价": "¥2100",
            },
            "ai_analysis": {
                "analysis_source": "keyword",
                "is_recommended": False,
                "reason": "未命中规则",
            },
        },
    ]
    _write_jsonl(jsonl_dir / "apple_watch_full_data.jsonl", records)

    response = client.get("/api/dashboard/summary")
    assert response.status_code == 200
    payload = response.json()

    assert payload["summary"]["enabled_tasks"] == 2
    assert payload["summary"]["running_tasks"] == 1
    assert payload["summary"]["result_files"] == 1
    assert payload["summary"]["scanned_items"] == 2
    assert payload["summary"]["recommended_items"] == 1
    assert payload["summary"]["ai_recommended_items"] == 1
    assert payload["summary"]["keyword_recommended_items"] == 0
    assert payload["focus_file"] == "apple_watch_full_data.jsonl"

    watch_summary = next(
        item for item in payload["task_summaries"] if item["task_name"] == "Apple Watch 任务"
    )
    assert watch_summary["filename"] == "apple_watch_full_data.jsonl"
    assert watch_summary["total_items"] == 2
    assert watch_summary["latest_recommended_title"] == "Apple Watch S10"

    ipad_summary = next(
        item for item in payload["task_summaries"] if item["task_name"] == "iPad 任务"
    )
    assert ipad_summary["filename"] is None
    assert ipad_summary["is_running"] is True

    statuses = {item["status"] for item in payload["recent_activities"]}
    assert "AI 推荐" in statuses
    assert "结果已更新" in statuses
    assert "运行中" in statuses


def test_dashboard_maps_task_owned_results_by_id_for_duplicate_tasks(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "app.sqlite3"
    monkeypatch.setenv("APP_DATABASE_FILE", str(database_path))

    repository = SqliteTaskRepository(
        db_path=str(database_path),
        legacy_config_file=None,
    )
    task_service = TaskService(repository)
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[deps.get_task_service] = lambda: task_service

    duplicate = TaskCreate(
        task_name="重复任务",
        keyword="camera",
        description="关注价格合适的相机。",
        max_pages=2,
        personal_only=True,
    )
    first = asyncio.run(
        task_service.create_ai_task_with_criteria(duplicate, "first criteria")
    )
    second = asyncio.run(
        task_service.create_ai_task_with_criteria(duplicate, "second criteria")
    )

    def task_record(item_id, title, crawl_time):
        return {
            "爬取时间": crawl_time,
            "搜索关键字": "historical keyword",
            "任务名称": "historical task name",
            "商品信息": {
                "商品ID": item_id,
                "商品标题": title,
                "商品链接": f"https://www.goofish.com/item?id={item_id}",
                "当前售价": "¥1000",
            },
            "ai_analysis": {
                "analysis_source": "keyword",
                "is_recommended": True,
            },
        }

    asyncio.run(
        save_task_result_record(
            task_record("camera-1", "First camera", "2026-07-14T10:00:00"),
            first.keyword,
            first.id,
        )
    )
    asyncio.run(
        save_task_result_record(
            task_record("camera-2", "Second camera", "2026-07-14T11:00:00"),
            second.keyword,
            second.id,
        )
    )

    response = TestClient(app).get("/api/dashboard/summary")
    assert response.status_code == 200
    payload = response.json()

    summaries = {item["task_id"]: item for item in payload["task_summaries"]}
    assert set(summaries) == {first.id, second.id}
    assert summaries[first.id]["filename"] == f"task_{first.id}_full_data.jsonl"
    assert summaries[second.id]["filename"] == f"task_{second.id}_full_data.jsonl"
    assert summaries[first.id]["latest_recommended_title"] == "First camera"
    assert summaries[second.id]["latest_recommended_title"] == "Second camera"
    assert all(item["task_name"] == "重复任务" for item in summaries.values())
    assert all(item["keyword"] == "camera" for item in summaries.values())
    assert payload["summary"]["result_files"] == 2
    assert payload["summary"]["scanned_items"] == 2


def test_dashboard_handles_structurally_malformed_latest_result(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "app.sqlite3"
    monkeypatch.setenv("APP_DATABASE_FILE", str(database_path))
    repository = SqliteTaskRepository(
        db_path=str(database_path),
        legacy_config_file=None,
    )
    task_service = TaskService(repository)
    task = asyncio.run(
        task_service.create_ai_task_with_criteria(
            TaskCreate(
                task_name="Malformed result task",
                keyword="fictional",
                description="Fictional criteria only.",
                max_pages=1,
                personal_only=True,
            ),
            "fictional criteria",
        )
    )
    malformed = {"商品信息": [], "ai_analysis": {"is_recommended": True}}
    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO result_items (
                task_id, result_filename, keyword, task_name, crawl_time,
                item_id, title, link_unique_key, is_recommended,
                analysis_source, keyword_hit_count, status, search_text, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'ai', 0, 'active', '', ?)
            """,
            (
                task.id,
                build_task_result_filename(task.id),
                task.keyword,
                task.task_name,
                "2026-07-14T12:00:00",
                "malformed-latest",
                "Structured fallback title",
                "item:malformed-latest",
                json.dumps(malformed, ensure_ascii=False),
            ),
        )
        conn.commit()

    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[deps.get_task_service] = lambda: task_service
    with TestClient(app) as client:
        response = client.get("/api/dashboard/summary")

    assert response.status_code == 200
    payload = response.json()
    summary = next(item for item in payload["task_summaries"] if item["task_id"] == task.id)
    assert summary["total_items"] == 1
    assert summary["recommended_items"] == 1
    assert summary["latest_recommended_title"] is None
    assert payload["summary"]["scanned_items"] == 1
