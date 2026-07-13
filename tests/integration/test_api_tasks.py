import asyncio
import time


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
