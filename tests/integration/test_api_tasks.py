import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api import dependencies as deps
from src.api.routes import tasks as tasks_route
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
from src.services.task_prompt_service import TaskPromptStore
from src.utils import resolve_task_log_path


def _owned_result_record(title: str) -> dict:
    return {
        "爬取时间": "2026-07-14T12:00:00",
        "搜索关键字": "same keyword",
        "任务名称": "same task",
        "商品信息": {
            "商品ID": "shared",
            "商品标题": title,
            "商品链接": "https://www.goofish.com/item?id=shared",
            "当前售价": "100",
        },
        "ai_analysis": {"analysis_source": "ai", "is_recommended": True},
    }


def _all_owned_records(task_id: int) -> list[dict]:
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


def _all_legacy_records() -> list[dict]:
    return asyncio.run(
        load_all_result_records(
            "same_keyword_full_data.jsonl",
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=True,
        )
    )


def _seed_task_deletion_data(
    api_client,
    api_context,
    sample_task_payload,
    monkeypatch,
) -> dict:
    monkeypatch.setenv("APP_DATABASE_FILE", str(api_context["db_path"]))
    first_payload = dict(sample_task_payload)
    first_payload.update(task_name="same task", keyword="same keyword")
    first_response = api_client.post("/api/tasks/", json=first_payload)
    second_response = api_client.post("/api/tasks/", json=first_payload)
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_id = first_response.json()["task"]["id"]
    second_id = second_response.json()["task"]["id"]

    asyncio.run(
        save_task_result_record(
            _owned_result_record("first task item"),
            "same keyword",
            first_id,
        )
    )
    asyncio.run(
        save_task_result_record(
            _owned_result_record("second task item"),
            "same keyword",
            second_id,
        )
    )
    asyncio.run(save_result_record(_owned_result_record("legacy item"), "same keyword"))
    for task_id, price in ((first_id, "100"), (second_id, "900")):
        record_market_snapshots(
            task_id=task_id,
            keyword="same keyword",
            task_name="same task",
            items=[{"商品ID": "shared", "当前售价": price}],
            run_id=f"run-{task_id}",
        )
    record_market_snapshots(
        keyword="same keyword",
        task_name="legacy",
        items=[{"商品ID": "shared", "当前售价": "500"}],
        run_id="legacy-run",
    )
    asyncio.run(save_task_result_blacklist_keywords(first_id, ["first-only"]))
    asyncio.run(save_task_result_blacklist_keywords(second_id, ["second-only"]))

    log_path = Path(resolve_task_log_path(first_id, "same task"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("fictional log", encoding="utf-8")
    prompt_path = TaskPromptStore().criteria_path(first_id)
    assert prompt_path.exists()
    return {
        "first_id": first_id,
        "second_id": second_id,
        "log_path": log_path,
        "prompt_path": prompt_path,
    }


def _assert_sibling_and_legacy_data_remain(second_id: int) -> None:
    assert len(_all_owned_records(second_id)) == 1
    assert len(_all_legacy_records()) == 1
    assert len(load_task_price_snapshots(second_id)) == 1
    assert len(load_price_snapshots("same keyword")) == 1
    assert asyncio.run(load_task_result_blacklist_keywords(second_id)) == [
        "second-only"
    ]


def test_create_list_update_delete_task(api_client, api_context, sample_task_payload):
    response = api_client.post("/api/tasks/", json=sample_task_payload)
    assert response.status_code == 200
    created = response.json()["task"]
    task_id = created["id"]
    assert created["task_name"] == sample_task_payload["task_name"]
    assert created["analyze_images"] is True
    assert created["next_run_at"] == "2026-03-19T08:15:00+08:00"

    response = api_client.get("/api/tasks")
    assert response.status_code == 200
    tasks = response.json()
    assert len(tasks) == 1
    assert tasks[0]["keyword"] == sample_task_payload["keyword"]
    assert tasks[0]["analyze_images"] is True
    assert tasks[0]["next_run_at"] == "2026-03-19T08:15:00+08:00"

    response = api_client.patch(
        f"/api/tasks/{task_id}",
        json={"enabled": False, "analyze_images": False},
    )
    assert response.status_code == 200
    updated = response.json()["task"]
    assert updated["enabled"] is False
    assert updated["analyze_images"] is False
    assert updated["next_run_at"] is None

    response = api_client.delete(f"/api/tasks/{task_id}")
    assert response.status_code == 200

    response = api_client.get("/api/tasks")
    assert response.status_code == 200
    assert response.json() == []


def test_start_stop_task_updates_status(api_client, api_context, sample_task_payload):
    response = api_client.post("/api/tasks/", json=sample_task_payload)
    assert response.status_code == 200
    task_id = response.json()["task"]["id"]

    response = api_client.post(f"/api/tasks/start/{task_id}")
    assert response.status_code == 200

    response = api_client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json()["is_running"] is True

    response = api_client.post(f"/api/tasks/stop/{task_id}")
    assert response.status_code == 200

    response = api_client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json()["is_running"] is False

    process_service = api_context["process_service"]
    assert process_service.started == [(task_id, sample_task_payload["task_name"])]
    assert process_service.stopped == [task_id]


def test_task_preflight_endpoint_returns_structured_report(
    api_client,
    api_context,
    sample_task_payload,
):
    created = api_client.post("/api/tasks/", json=sample_task_payload)
    task_id = created.json()["task"]["id"]
    report = SimpleNamespace(
        to_dict=lambda: {
            "task_id": task_id,
            "task_name": sample_task_payload["task_name"],
            "success": True,
            "failure_kind": "success",
            "failed_stage": None,
            "reason": "运行环境预检通过",
            "suggestion": "可以开始正式监控",
            "stages": [
                {
                    "key": "search_source",
                    "label": "搜索数据源识别",
                    "status": "success",
                    "message": "已捕获可解析的闲鱼商品数据",
                }
            ],
        }
    )

    class FakePreflightService:
        async def run(self, task):
            assert task.id == task_id
            return report

    api_context["app"].dependency_overrides[
        deps.get_monitoring_preflight_service
    ] = lambda: FakePreflightService()

    response = api_client.post(f"/api/tasks/preflight/{task_id}")

    assert response.status_code == 200
    payload = response.json()["preflight"]
    assert payload["success"] is True
    assert payload["stages"][0]["key"] == "search_source"


def test_task_start_returns_preflight_failure_without_spawning(
    api_client,
    api_context,
    sample_task_payload,
    monkeypatch,
):
    created = api_client.post("/api/tasks/", json=sample_task_payload)
    task_id = created.json()["task"]["id"]
    process_service = api_context["process_service"]
    report = {
        "task_id": task_id,
        "success": False,
        "failure_kind": "proxy_unreachable",
        "failed_stage": "proxy_connect",
        "reason": "代理端点不可连接",
        "suggestion": "确认本机代理正在运行",
        "stages": [],
    }

    async def blocked_start(received_task_id, _task_name):
        assert received_task_id == task_id
        return False

    monkeypatch.setattr(process_service, "start_task", blocked_start)
    monkeypatch.setattr(
        process_service,
        "get_last_preflight_report",
        lambda received_task_id: report if received_task_id == task_id else None,
        raising=False,
    )

    response = api_client.post(f"/api/tasks/start/{task_id}")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "preflight_failed"
    assert detail["preflight"]["failed_stage"] == "proxy_connect"
    assert process_service.started == []


def test_generate_keyword_mode_task_without_ai_criteria(api_client):
    payload = {
        "task_name": "A7M4 关键词筛选",
        "keyword": "sony a7m4",
        "description": "",
        "decision_mode": "keyword",
        "keyword_rules": ["a7m4", "验货宝"],
        "max_pages": 2,
        "personal_only": True,
    }

    response = api_client.post("/api/tasks/generate", json=payload)
    assert response.status_code == 200
    created = response.json()["task"]
    assert created["decision_mode"] == "keyword"
    assert created["ai_prompt_criteria_file"] == ""
    assert created["keyword_rules"] == ["a7m4", "验货宝"]


def test_direct_create_rejects_ai_task_without_valid_criteria(
    api_client,
    api_context,
    sample_task_payload,
):
    payload = dict(sample_task_payload)
    payload["ai_prompt_criteria_file"] = ""

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 400
    assert "criteria" in response.json()["detail"]
    assert api_client.get("/api/tasks").json() == []
    assert not (api_context["db_path"].parent / "prompts" / "tasks").exists()


def test_direct_create_allows_keyword_task_without_criteria(api_client):
    payload = {
        "task_name": "Keyword-only task",
        "keyword": "camera",
        "description": "",
        "decision_mode": "keyword",
        "keyword_rules": ["camera"],
        "ai_prompt_criteria_file": "",
    }

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["decision_mode"] == "keyword"
    assert task["ai_prompt_criteria_file"] == ""


def test_generate_ai_task_returns_job_and_completes_async(api_client, api_context, monkeypatch):
    payload = {
        "task_name": "Apple Watch S10",
        "keyword": "apple watch s10",
        "description": "只看国行蜂窝版，电池健康高于 95%，拒绝维修机。",
        "analyze_images": False,
        "decision_mode": "ai",
        "max_pages": 2,
        "personal_only": True,
    }

    async def fake_generate_criteria(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return "[V6.3 核心升级]\\nApple Watch criteria"

    monkeypatch.setattr(
        "src.services.task_generation_runner.generate_criteria",
        fake_generate_criteria,
    )

    response = api_client.post("/api/tasks/generate", json=payload)

    assert response.status_code == 202
    job = response.json()["job"]
    assert isinstance(job["job_id"], str)
    assert job["status"] in {"queued", "running"}
    assert job["task"] is None

    status_response = api_client.get(f"/api/tasks/generate-jobs/{job['job_id']}")
    assert status_response.status_code == 200

    for _ in range(50):
        status_response = api_client.get(f"/api/tasks/generate-jobs/{job['job_id']}")
        latest_job = status_response.json()["job"]
        if latest_job["status"] == "completed":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("任务生成作业未在预期时间内完成")

    assert latest_job["task"]["task_name"] == payload["task_name"]
    task_id = latest_job["task"]["id"]
    criteria_path = latest_job["task"]["ai_prompt_criteria_file"]
    assert criteria_path == f"prompts/tasks/{task_id}/criteria.txt"
    assert (api_context["db_path"].parent / criteria_path).read_text(
        encoding="utf-8"
    ) == "[V6.3 核心升级]\\nApple Watch criteria"
    assert latest_job["task"]["analyze_images"] is False
    assert api_context["scheduler_service"].reload_calls == 1


def test_create_task_accepts_cron_alias(api_client, sample_task_payload):
    payload = dict(sample_task_payload)
    payload["cron"] = "@daily"

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 200
    assert response.json()["task"]["cron"] == "0 0 * * *"


def test_create_task_rejects_fixed_account_strategy_without_state_file(api_client, sample_task_payload):
    payload = dict(sample_task_payload)
    payload["account_strategy"] = "fixed"

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 422


def test_create_task_accepts_rotate_account_strategy(api_client, sample_task_payload):
    payload = dict(sample_task_payload)
    payload["account_strategy"] = "rotate"

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["account_strategy"] == "rotate"


def test_task_id_is_read_only_and_prompt_path_stays_id_scoped(
    api_client, sample_task_payload
):
    created_response = api_client.post("/api/tasks/", json=sample_task_payload)
    assert created_response.status_code == 200
    created = created_response.json()["task"]

    response = api_client.patch(
        f"/api/tasks/{created['id']}",
        json={
            "id": 999999,
            "task_name": "renamed task",
            "keyword": "renamed keyword",
            "ai_prompt_criteria_file": "prompts/untrusted.txt",
        },
    )

    assert response.status_code == 200
    updated = response.json()["task"]
    assert updated["id"] == created["id"]
    assert updated["ai_prompt_criteria_file"] == (
        f"prompts/tasks/{created['id']}/criteria.txt"
    )


def test_duplicate_task_names_remain_distinct_by_id(api_client, sample_task_payload):
    first_response = api_client.post("/api/tasks/", json=sample_task_payload)
    second_response = api_client.post("/api/tasks/", json=sample_task_payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first = first_response.json()["task"]
    second = second_response.json()["task"]
    assert first["task_name"] == second["task_name"]
    assert first["id"] != second["id"]
    assert first["ai_prompt_criteria_file"] != second["ai_prompt_criteria_file"]


def test_update_task_accepts_six_field_cron_expression(api_client, sample_task_payload):
    create_response = api_client.post("/api/tasks/", json=sample_task_payload)
    assert create_response.status_code == 200
    task_id = create_response.json()["task"]["id"]

    response = api_client.patch(f"/api/tasks/{task_id}", json={"cron": "0 0 8 * * *"})

    assert response.status_code == 200

    task_response = api_client.get(f"/api/tasks/{task_id}")
    assert task_response.status_code == 200
    assert task_response.json()["cron"] == "0 0 8 * * *"


def test_create_task_rejects_invalid_cron_expression(api_client, sample_task_payload):
    payload = dict(sample_task_payload)
    payload["cron"] = "every day at 8"

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 422


def test_delete_task_stops_only_deleted_runtime(
    api_client,
    api_context,
    sample_task_payload,
):
    second_payload = dict(sample_task_payload)
    second_payload["task_name"] = "Sony A7CR"
    second_payload["keyword"] = "sony a7cr"
    second_payload["ai_prompt_criteria_file"] = "prompts/sony_a7cr_criteria.txt"

    first_response = api_client.post("/api/tasks/", json=sample_task_payload)
    second_response = api_client.post("/api/tasks/", json=second_payload)
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_id = first_response.json()["task"]["id"]
    second_id = second_response.json()["task"]["id"]
    assert api_client.post(f"/api/tasks/start/{first_id}").status_code == 200

    response = api_client.delete(f"/api/tasks/{first_id}")

    assert response.status_code == 200
    process_service = api_context["process_service"]
    assert process_service.stopped == [first_id]

    remaining = api_client.get(f"/api/tasks/{second_id}")
    assert remaining.status_code == 200
    assert remaining.json()["id"] == second_id


def test_delete_task_cleans_only_its_owned_data(
    api_client,
    api_context,
    sample_task_payload,
    monkeypatch,
):
    monkeypatch.setenv("APP_DATABASE_FILE", str(api_context["db_path"]))
    first_payload = dict(sample_task_payload)
    first_payload.update(task_name="same task", keyword="same keyword")
    second_payload = dict(first_payload)

    first_response = api_client.post("/api/tasks/", json=first_payload)
    second_response = api_client.post("/api/tasks/", json=second_payload)
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_id = first_response.json()["task"]["id"]
    second_id = second_response.json()["task"]["id"]

    asyncio.run(
        save_task_result_record(
            _owned_result_record("first task item"),
            "same keyword",
            first_id,
        )
    )
    asyncio.run(
        save_task_result_record(
            _owned_result_record("second task item"),
            "same keyword",
            second_id,
        )
    )
    asyncio.run(save_result_record(_owned_result_record("legacy item"), "same keyword"))
    for task_id, price in ((first_id, "100"), (second_id, "900")):
        record_market_snapshots(
            task_id=task_id,
            keyword="same keyword",
            task_name="same task",
            items=[{"商品ID": "shared", "当前售价": price}],
            run_id=f"run-{task_id}",
        )
    record_market_snapshots(
        keyword="same keyword",
        task_name="legacy",
        items=[{"商品ID": "shared", "当前售价": "500"}],
        run_id="legacy-run",
    )
    asyncio.run(save_task_result_blacklist_keywords(first_id, ["first-only"]))
    asyncio.run(save_task_result_blacklist_keywords(second_id, ["second-only"]))

    log_path = resolve_task_log_path(first_id, "same task")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(log_path).write_text("fictional log", encoding="utf-8")
    prompt_path = TaskPromptStore().criteria_path(first_id)
    assert prompt_path.exists()

    response = api_client.delete(f"/api/tasks/{first_id}")

    assert response.status_code == 200
    assert _all_owned_records(first_id) == []
    assert len(_all_owned_records(second_id)) == 1
    assert len(_all_legacy_records()) == 1
    assert load_task_price_snapshots(first_id) == []
    assert len(load_task_price_snapshots(second_id)) == 1
    assert len(load_price_snapshots("same keyword")) == 1
    assert asyncio.run(load_task_result_blacklist_keywords(first_id)) == []
    assert asyncio.run(load_task_result_blacklist_keywords(second_id)) == [
        "second-only"
    ]
    assert not prompt_path.exists()
    assert not Path(log_path).exists()


def test_delete_task_aborts_when_process_remains_running(
    api_client,
    api_context,
    sample_task_payload,
    monkeypatch,
):
    seeded = _seed_task_deletion_data(
        api_client,
        api_context,
        sample_task_payload,
        monkeypatch,
    )
    task_id = seeded["first_id"]
    process_service = api_context["process_service"]
    process_service.stop_result = False
    process_service.keep_running_after_stop = True
    process_service.running.add(task_id)
    scheduler_calls = api_context["scheduler_service"].reload_calls

    response = api_client.delete(f"/api/tasks/{task_id}")

    assert response.status_code == 409
    assert api_client.get(f"/api/tasks/{task_id}").status_code == 200
    assert len(_all_owned_records(task_id)) == 1
    assert len(load_task_price_snapshots(task_id)) == 1
    assert asyncio.run(load_task_result_blacklist_keywords(task_id)) == ["first-only"]
    assert seeded["prompt_path"].exists()
    assert seeded["log_path"].exists()
    assert api_context["scheduler_service"].reload_calls == scheduler_calls
    _assert_sibling_and_legacy_data_remain(seeded["second_id"])


def test_task_record_delete_failure_preserves_all_owned_data(
    api_client,
    api_context,
    sample_task_payload,
    monkeypatch,
    capsys,
):
    seeded = _seed_task_deletion_data(
        api_client,
        api_context,
        sample_task_payload,
        monkeypatch,
    )
    task_id = seeded["first_id"]
    scheduler_calls = api_context["scheduler_service"].reload_calls

    async def fail_task_record_delete(_task_id):
        raise RuntimeError("private-database-detail")

    monkeypatch.setattr(
        api_context["task_service"],
        "delete_task_record",
        fail_task_record_delete,
    )

    response = api_client.delete(f"/api/tasks/{task_id}")
    output = capsys.readouterr().out

    assert response.status_code == 500
    assert "private-database-detail" not in response.text
    assert "private-database-detail" not in output
    assert api_client.get(f"/api/tasks/{task_id}").status_code == 200
    assert len(_all_owned_records(task_id)) == 1
    assert len(load_task_price_snapshots(task_id)) == 1
    assert asyncio.run(load_task_result_blacklist_keywords(task_id)) == ["first-only"]
    assert seeded["prompt_path"].exists()
    assert seeded["log_path"].exists()
    assert api_context["scheduler_service"].reload_calls == scheduler_calls
    _assert_sibling_and_legacy_data_remain(seeded["second_id"])


@pytest.mark.parametrize("stop_result", [False, True])
def test_delete_task_continues_once_process_is_confirmed_stopped(
    stop_result,
    api_client,
    api_context,
    sample_task_payload,
):
    response = api_client.post("/api/tasks/", json=sample_task_payload)
    task_id = response.json()["task"]["id"]
    process_service = api_context["process_service"]
    process_service.stop_result = stop_result

    response = api_client.delete(f"/api/tasks/{task_id}")

    assert response.status_code == 200
    assert process_service.is_running(task_id) is False
    assert api_client.get(f"/api/tasks/{task_id}").status_code == 404


def test_prompt_cleanup_failure_does_not_block_other_cleanup(
    api_client,
    api_context,
    sample_task_payload,
    monkeypatch,
    capsys,
):
    seeded = _seed_task_deletion_data(
        api_client,
        api_context,
        sample_task_payload,
        monkeypatch,
    )
    task_id = seeded["first_id"]
    scheduler_calls = api_context["scheduler_service"].reload_calls

    async def fail_prompt_cleanup(_task_id):
        raise RuntimeError("private-prompt-path-and-secret")

    monkeypatch.setattr(
        api_context["task_service"],
        "delete_task_prompt",
        fail_prompt_cleanup,
    )

    response = api_client.delete(f"/api/tasks/{task_id}")
    output = capsys.readouterr().out

    assert response.status_code == 200
    assert "private-prompt-path-and-secret" not in response.text
    assert "private-prompt-path-and-secret" not in output
    assert f"task_id={task_id}" in output
    assert "resource=prompt" in output
    assert "error=RuntimeError" in output
    assert seeded["prompt_path"].exists()
    assert _all_owned_records(task_id) == []
    assert load_task_price_snapshots(task_id) == []
    assert asyncio.run(load_task_result_blacklist_keywords(task_id)) == []
    assert not seeded["log_path"].exists()
    assert api_context["scheduler_service"].reload_calls == scheduler_calls + 1
    _assert_sibling_and_legacy_data_remain(seeded["second_id"])


def test_result_cleanup_failure_does_not_block_other_cleanup(
    api_client,
    api_context,
    sample_task_payload,
    monkeypatch,
    capsys,
):
    seeded = _seed_task_deletion_data(
        api_client,
        api_context,
        sample_task_payload,
        monkeypatch,
    )
    task_id = seeded["first_id"]

    async def fail_result_cleanup(_task_id):
        raise RuntimeError("private-result-record")

    monkeypatch.setattr(
        tasks_route,
        "delete_task_result_records",
        fail_result_cleanup,
    )

    response = api_client.delete(f"/api/tasks/{task_id}")
    output = capsys.readouterr().out

    assert response.status_code == 200
    assert "private-result-record" not in response.text
    assert "private-result-record" not in output
    assert len(_all_owned_records(task_id)) == 1
    assert load_task_price_snapshots(task_id) == []
    assert asyncio.run(load_task_result_blacklist_keywords(task_id)) == []
    assert not seeded["prompt_path"].exists()
    assert not seeded["log_path"].exists()
    _assert_sibling_and_legacy_data_remain(seeded["second_id"])


def test_log_cleanup_failure_happens_after_other_cleanup(
    api_client,
    api_context,
    sample_task_payload,
    monkeypatch,
    capsys,
):
    seeded = _seed_task_deletion_data(
        api_client,
        api_context,
        sample_task_payload,
        monkeypatch,
    )
    task_id = seeded["first_id"]

    def fail_log_cleanup(_path):
        raise PermissionError("private-log-path")

    monkeypatch.setattr(tasks_route.os, "remove", fail_log_cleanup)

    response = api_client.delete(f"/api/tasks/{task_id}")
    output = capsys.readouterr().out

    assert response.status_code == 200
    assert "private-log-path" not in response.text
    assert "private-log-path" not in output
    assert seeded["log_path"].exists()
    assert not seeded["prompt_path"].exists()
    assert _all_owned_records(task_id) == []
    assert load_task_price_snapshots(task_id) == []
    assert asyncio.run(load_task_result_blacklist_keywords(task_id)) == []
    _assert_sibling_and_legacy_data_remain(seeded["second_id"])
