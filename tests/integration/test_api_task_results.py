import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import dependencies as deps
from src.api.routes import results
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import build_task_result_filename
from src.services.price_history_service import (
    load_price_snapshots,
    load_task_price_snapshots,
    record_market_snapshots,
)
from src.services.result_storage_service import (
    load_all_result_records,
    load_all_task_result_records,
    load_task_result_blacklist_keywords,
    save_result_record,
    save_task_result_blacklist_keywords,
    save_task_result_record,
)


class FakeTaskService:
    def __init__(self):
        self.tasks = {
            101: SimpleNamespace(id=101, task_name="Same", keyword="camera"),
            102: SimpleNamespace(id=102, task_name="Same", keyword="camera"),
        }

    async def get_task(self, task_id: int):
        return self.tasks.get(task_id)


def _record(*, item_id: str, title: str, task_name: str) -> dict:
    return {
        "爬取时间": "2026-07-14T10:00:00",
        "搜索关键字": "camera",
        "任务名称": task_name,
        "商品信息": {
            "商品ID": item_id,
            "商品标题": title,
            "商品链接": f"https://www.goofish.com/item?id={item_id}",
            "当前售价": "100",
        },
        "ai_analysis": {
            "analysis_source": "ai",
            "is_recommended": True,
            "reason": "fictional recommendation",
        },
    }


def _load_task_records(task_id: int) -> list[dict]:
    return asyncio.run(
        load_all_task_result_records(
            task_id,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=True,
        )
    )


def _load_legacy_records() -> list[dict]:
    return asyncio.run(
        load_all_result_records(
            "camera_full_data.jsonl",
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=True,
        )
    )


@pytest.fixture()
def task_results_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    service = FakeTaskService()
    app = FastAPI()
    app.include_router(results.router)
    app.dependency_overrides[deps.get_task_service] = lambda: service
    client = TestClient(app)
    yield client, service
    client.close()


def _seed_isolated_data():
    asyncio.run(
        save_task_result_record(
            _record(item_id="shared", title="Task A Camera", task_name="Same"),
            "camera",
            101,
        )
    )
    asyncio.run(
        save_task_result_record(
            _record(item_id="shared", title="Task B Camera", task_name="Same"),
            "camera",
            102,
        )
    )
    asyncio.run(
        save_result_record(
            _record(item_id="shared", title="Legacy Camera", task_name="Legacy"),
            "camera",
        )
    )
    record_market_snapshots(
        task_id=101,
        keyword="camera",
        task_name="Same",
        items=[{"商品ID": "shared", "商品标题": "A", "当前售价": "100"}],
        run_id="run-a",
        snapshot_time="2026-07-14T10:00:00",
    )
    record_market_snapshots(
        task_id=102,
        keyword="camera",
        task_name="Same",
        items=[{"商品ID": "shared", "商品标题": "B", "当前售价": "900"}],
        run_id="run-b",
        snapshot_time="2026-07-14T10:00:00",
    )
    record_market_snapshots(
        keyword="camera",
        task_name="Legacy",
        items=[{"商品ID": "shared", "商品标题": "Legacy", "当前售价": "500"}],
        run_id="run-legacy",
        snapshot_time="2026-07-14T10:00:00",
    )


def _insert_raw_task_result(
    *,
    task_id: int,
    item_id: str,
    crawl_time: str,
    raw_record: dict,
    is_recommended: bool = False,
) -> None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO result_items (
                task_id, result_filename, keyword, task_name, crawl_time,
                item_id, title, link_unique_key, is_recommended,
                analysis_source, keyword_hit_count, status, search_text, raw_json
            ) VALUES (?, ?, 'camera', 'Same', ?, ?, ?, ?, ?, 'ai', 0,
                      'active', '', ?)
            """,
            (
                task_id,
                build_task_result_filename(task_id),
                crawl_time,
                item_id,
                f"Structured {item_id}",
                f"item:{item_id}",
                int(is_recommended),
                json.dumps(raw_record, ensure_ascii=False),
            ),
        )
        conn.commit()


def test_task_scoped_api_and_canonical_filename_keep_same_keyword_tasks_isolated(
    task_results_client,
):
    client, _ = task_results_client
    _seed_isolated_data()

    task_a = client.get("/api/results/tasks/101")
    task_b = client.get("/api/results/tasks/102")
    canonical = client.get("/api/results/task_101_full_data.jsonl")
    legacy = client.get("/api/results/camera_full_data.jsonl")

    assert task_a.status_code == 200
    assert task_a.json()["filename"] == "task_101_full_data.jsonl"
    assert [item["商品信息"]["商品标题"] for item in task_a.json()["items"]] == [
        "Task A Camera"
    ]
    assert [item["商品信息"]["商品标题"] for item in task_b.json()["items"]] == [
        "Task B Camera"
    ]
    assert canonical.json()["items"][0]["任务ID"] == 101
    assert legacy.json()["items"][0]["商品信息"]["商品标题"] == "Legacy Camera"

    assert client.get("/api/results/tasks/999").status_code == 404
    assert client.get("/api/results/task_0101_full_data.jsonl").status_code == 400
    export = client.get("/api/results/tasks/101/export")
    assert export.status_code == 200
    assert "Task A Camera" in export.text
    assert "Task B Camera" not in export.text

    task_a_insights = client.get("/api/results/tasks/101/insights").json()
    task_b_insights = client.get("/api/results/tasks/102/insights").json()
    assert task_a_insights["market_summary"]["avg_price"] == 100.0
    assert task_b_insights["market_summary"]["avg_price"] == 900.0


def test_task_scoped_blacklist_and_status_updates_do_not_cross_tasks(
    task_results_client,
):
    client, _ = task_results_client
    _seed_isolated_data()

    rules = client.put(
        "/api/results/tasks/101/blacklist-rules",
        json={"keywords": ["Task A"]},
    )
    assert rules.status_code == 200
    assert rules.json()["keywords"] == ["task a"]
    assert client.get("/api/results/tasks/101").json()["total_items"] == 0
    assert client.get("/api/results/tasks/102").json()["total_items"] == 1

    hidden = client.patch(
        "/api/results/tasks/102/items/shared/status",
        json={"status": "hidden"},
    )
    assert hidden.status_code == 200
    assert client.get("/api/results/tasks/102").json()["total_items"] == 0
    assert _load_task_records(101)[0]["_status"] == "active"


def test_delete_task_results_preserves_sibling_and_legacy_data(task_results_client):
    client, _ = task_results_client
    _seed_isolated_data()
    asyncio.run(save_task_result_blacklist_keywords(101, ["private-a-rule"]))
    asyncio.run(save_task_result_blacklist_keywords(102, ["private-b-rule"]))

    response = client.delete("/api/results/tasks/101")

    assert response.status_code == 200
    assert response.json()["deleted_results"] == 1
    assert _load_task_records(101) == []
    assert len(_load_task_records(102)) == 1
    assert len(_load_legacy_records()) == 1
    assert load_task_price_snapshots(101) == []
    assert len(load_task_price_snapshots(102)) == 1
    assert len(load_price_snapshots("camera")) == 1
    assert asyncio.run(load_task_result_blacklist_keywords(101)) == []
    assert asyncio.run(load_task_result_blacklist_keywords(102)) == ["private-b-rule"]


def test_task_result_api_skips_structurally_malformed_rows_without_500(
    task_results_client,
):
    client, _ = task_results_client
    malformed_records = [
        {"商品信息": []},
        {"商品信息": "invalid"},
        {"ai_analysis": []},
        {"卖家信息": []},
    ]
    for index, record in enumerate(malformed_records):
        _insert_raw_task_result(
            task_id=101,
            item_id=f"malformed-{index}",
            crawl_time=f"2026-07-14T10:00:0{index}",
            raw_record=record,
            is_recommended=index == len(malformed_records) - 1,
        )

    for include_hidden in (False, True):
        response = client.get(
            "/api/results/tasks/101",
            params={"include_hidden": include_hidden},
        )
        assert response.status_code == 200
        assert response.json()["total_items"] == 4
        assert response.json()["items"] == []

    export = client.get(
        "/api/results/tasks/101/export",
        params={"include_hidden": True},
    )
    assert export.status_code == 200
    assert "任务名称,搜索关键字,商品ID,商品标题" in export.text

    download = client.get("/api/results/files/task_101_full_data.jsonl")
    assert download.status_code == 200
    assert download.text.splitlines() == [
        json.dumps(record, ensure_ascii=False) for record in malformed_records
    ]


def test_task_result_api_returns_empty_page_for_huge_page_number(
    task_results_client,
):
    client, _ = task_results_client
    _seed_isolated_data()
    huge_page = 10**19

    response = client.get(
        "/api/results/tasks/101",
        params={"page": huge_page, "limit": 20},
    )

    assert response.status_code == 200
    assert response.json() == {
        "task_id": 101,
        "filename": "task_101_full_data.jsonl",
        "total_items": 1,
        "page": huge_page,
        "limit": 20,
        "items": [],
    }
