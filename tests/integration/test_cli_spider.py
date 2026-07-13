import asyncio
import importlib
import json
import signal
import sys
import types

import pytest


class FakeScrapeTaskFailed(Exception):
    def __init__(
        self,
        *,
        task_name: str,
        failure_kind: str,
        reason: str,
        processed_item_count: int,
    ):
        self.task_name = task_name
        self.failure_kind = failure_kind
        self.reason = reason
        self.processed_item_count = processed_item_count
        super().__init__(reason)


def _load_spider(monkeypatch):
    fake_scraper = types.ModuleType("src.scraper")

    async def placeholder_scrape(task_config, debug_limit):
        return 0

    fake_scraper.ScrapeTaskFailed = FakeScrapeTaskFailed
    fake_scraper.sanitize_failure_reason = lambda reason: str(reason).replace(
        "cli-secret", "[REDACTED]"
    )
    fake_scraper.scrape_xianyu = placeholder_scrape
    monkeypatch.setitem(sys.modules, "src.scraper", fake_scraper)
    sys.modules.pop("spider_v2", None)
    return importlib.import_module("spider_v2")


def _write_keyword_task_config(tmp_path, load_json_fixture):
    config_data = load_json_fixture("config.sample.json")
    config_data[0]["enabled"] = True
    config_data[0]["decision_mode"] = "keyword"
    config_data[0]["ai_prompt_text"] = ""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(config_data, ensure_ascii=False), encoding="utf-8"
    )
    return config_data[0]["task_name"], config_path


class _FakeStoredTask:
    def __init__(self, payload):
        self.payload = dict(payload)
        self.id = self.payload["id"]
        self.task_name = self.payload["task_name"]

    def model_dump(self):
        return dict(self.payload)


class _FakeGuard:
    def migrate_legacy_task_keys(self, _tasks):
        return {"migrated": 0, "ambiguous": 0}


def test_cli_runs_single_task_with_prompt(tmp_path, load_json_fixture, monkeypatch):
    spider_v2 = _load_spider(monkeypatch)
    config_data = load_json_fixture("config.sample.json")

    base_prompt = "Base prompt. " + ("x" * 120) + " {{CRITERIA_SECTION}}"
    criteria_prompt = "Criteria text for A7M4."

    base_path = tmp_path / "base_prompt.txt"
    criteria_path = tmp_path / "criteria_prompt.txt"
    base_path.write_text(base_prompt, encoding="utf-8")
    criteria_path.write_text(criteria_prompt, encoding="utf-8")

    config_data[0]["ai_prompt_base_file"] = str(base_path)
    config_data[0]["ai_prompt_criteria_file"] = str(criteria_path)

    config_data[1]["ai_prompt_base_file"] = str(base_path)
    config_data[1]["ai_prompt_criteria_file"] = str(criteria_path)

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")

    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))

    called = []

    async def fake_scrape_xianyu(task_config, debug_limit):
        called.append(task_config["task_name"])
        assert "{{CRITERIA_SECTION}}" not in task_config["ai_prompt_text"]
        assert "Criteria text for A7M4." in task_config["ai_prompt_text"]
        return 1

    monkeypatch.setattr(spider_v2, "scrape_xianyu", fake_scrape_xianyu)
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config_path), "--task-name", "Sony A7M4"])

    exit_code = asyncio.run(spider_v2.main())

    assert called == ["Sony A7M4"]
    assert exit_code == 0


def test_cli_runs_keyword_mode_without_prompt_files(tmp_path, load_json_fixture, monkeypatch):
    spider_v2 = _load_spider(monkeypatch)
    config_data = load_json_fixture("config.sample.json")
    config_data[0]["enabled"] = True
    config_data[0]["decision_mode"] = "keyword"
    config_data[0]["keyword_rules"] = ["a7m4", "验货宝"]
    config_data[0]["ai_prompt_base_file"] = "missing_base.txt"
    config_data[0]["ai_prompt_criteria_file"] = "missing_criteria.txt"

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")

    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))

    captured = []

    async def fake_scrape_xianyu(task_config, debug_limit):
        captured.append(task_config)
        return 1

    monkeypatch.setattr(spider_v2, "scrape_xianyu", fake_scrape_xianyu)
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--config", str(config_path), "--task-name", "Sony A7M4"])

    exit_code = asyncio.run(spider_v2.main())

    assert len(captured) == 1
    assert captured[0]["decision_mode"] == "keyword"
    assert captured[0]["ai_prompt_text"] == ""
    assert exit_code == 0


@pytest.mark.parametrize("task_id", [71, 72])
def test_cli_task_id_selects_exact_same_name_task(
    tmp_path, monkeypatch, task_id
):
    spider_v2 = _load_spider(monkeypatch)
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    stored_tasks = [
        _FakeStoredTask(
            {
                "id": current_id,
                "task_name": "duplicate-name",
                "enabled": True,
                "keyword": f"camera-{current_id}",
                "decision_mode": "keyword",
                "keyword_rules": ["camera"],
                "account_state_file": str(state_path),
            }
        )
        for current_id in (71, 72)
    ]

    class FakeRepository:
        async def find_by_id(self, requested_id):
            return next(
                (task for task in stored_tasks if task.id == requested_id),
                None,
            )

        async def find_all(self):
            return list(stored_tasks)

    called = []

    async def fake_scrape(task_config, debug_limit):
        called.append((task_config["id"], task_config["keyword"]))
        return 0

    monkeypatch.setattr(spider_v2, "SqliteTaskRepository", FakeRepository)
    monkeypatch.setattr(spider_v2, "FailureGuard", _FakeGuard)
    monkeypatch.setattr(spider_v2, "migrate_task_prompts", lambda: None)
    monkeypatch.setattr(spider_v2, "scrape_xianyu", fake_scrape)
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--task-id", str(task_id)])

    exit_code = asyncio.run(spider_v2.main())

    assert exit_code == 0
    assert called == [(task_id, f"camera-{task_id}")]


def test_cli_missing_task_id_fails_before_scraper_start(monkeypatch, capsys):
    spider_v2 = _load_spider(monkeypatch)

    class FakeRepository:
        async def find_by_id(self, _task_id):
            return None

        async def find_all(self):
            raise AssertionError("missing ID must stop before loading runnable tasks")

    async def unexpected_scrape(*_args, **_kwargs):
        raise AssertionError("scraper must not start for a missing task ID")

    monkeypatch.setattr(spider_v2, "SqliteTaskRepository", FakeRepository)
    monkeypatch.setattr(spider_v2, "migrate_task_prompts", lambda: None)
    monkeypatch.setattr(spider_v2, "scrape_xianyu", unexpected_scrape)
    monkeypatch.setattr(sys, "argv", ["spider_v2.py", "--task-id", "999"])

    exit_code = asyncio.run(spider_v2.main())
    output = capsys.readouterr().out

    assert exit_code != 0
    assert "未找到任务 ID 999" in output


def test_cli_deprecated_task_name_rejects_duplicate_matches(
    tmp_path, load_json_fixture, monkeypatch, capsys
):
    spider_v2 = _load_spider(monkeypatch)
    config_data = load_json_fixture("config.sample.json")[:2]
    for index, task in enumerate(config_data):
        task["id"] = index + 1
        task["task_name"] = "duplicate-name"
        task["enabled"] = True
        task["decision_mode"] = "keyword"
        task["keyword_rules"] = ["camera"]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data), encoding="utf-8")

    async def unexpected_scrape(*_args, **_kwargs):
        raise AssertionError("ambiguous legacy name must not start a task")

    monkeypatch.setattr(spider_v2, "scrape_xianyu", unexpected_scrape)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "spider_v2.py",
            "--config",
            str(config_path),
            "--task-name",
            "duplicate-name",
        ],
    )

    exit_code = asyncio.run(spider_v2.main())
    output = capsys.readouterr().out

    assert exit_code != 0
    assert "请改用 --task-id" in output


@pytest.mark.parametrize(
    ("failure_kind", "expected_status"),
    [
        ("risk_control", "风控终止"),
        ("login_required", "登录失效"),
        ("runtime_error", "运行异常"),
    ],
)
def test_cli_returns_nonzero_for_terminal_task_failures(
    tmp_path,
    load_json_fixture,
    monkeypatch,
    capsys,
    failure_kind,
    expected_status,
):
    spider_v2 = _load_spider(monkeypatch)
    task_name, config_path = _write_keyword_task_config(
        tmp_path, load_json_fixture
    )
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))

    async def fail_scrape(task_config, debug_limit):
        raise spider_v2.ScrapeTaskFailed(
            task_name=task_config["task_name"],
            failure_kind=failure_kind,
            reason="simulated failure",
            processed_item_count=1,
        )

    monkeypatch.setattr(spider_v2, "scrape_xianyu", fail_scrape)
    monkeypatch.setattr(
        sys,
        "argv",
        ["spider_v2.py", "--config", str(config_path), "--task-name", task_name],
    )

    exit_code = asyncio.run(spider_v2.main())
    output = capsys.readouterr().out

    assert exit_code != 0
    assert expected_status in output
    assert "正常结束" not in output


def test_cli_keeps_multi_task_concurrency_and_returns_nonzero_if_any_task_fails(
    tmp_path, load_json_fixture, monkeypatch, capsys
):
    spider_v2 = _load_spider(monkeypatch)
    config_data = load_json_fixture("config.sample.json")
    for task in config_data[:2]:
        task["enabled"] = True
        task["decision_mode"] = "keyword"
        task["ai_prompt_text"] = ""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(config_data, ensure_ascii=False), encoding="utf-8"
    )
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["spider_v2.py", "--config", str(config_path)],
    )
    started = []
    all_started = asyncio.Event()

    async def mixed_scrape(task_config, debug_limit):
        started.append(task_config["task_name"])
        if len(started) == 2:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        if task_config["task_name"] == config_data[1]["task_name"]:
            raise spider_v2.ScrapeTaskFailed(
                task_name=task_config["task_name"],
                failure_kind="risk_control",
                reason="simulated risk control",
                processed_item_count=0,
            )
        return 2

    monkeypatch.setattr(spider_v2, "scrape_xianyu", mixed_scrape)

    exit_code = asyncio.run(spider_v2.main())
    output = capsys.readouterr().out

    assert exit_code != 0
    assert set(started) == {
        config_data[0]["task_name"],
        config_data[1]["task_name"],
    }
    assert f"任务 '{config_data[0]['task_name']}' 正常结束" in output
    assert f"任务 '{config_data[1]['task_name']}' 风控终止" in output


def test_cli_redacts_unknown_exception_before_logging(
    tmp_path, load_json_fixture, monkeypatch, capsys
):
    spider_v2 = _load_spider(monkeypatch)
    task_name, config_path = _write_keyword_task_config(
        tmp_path, load_json_fixture
    )
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["spider_v2.py", "--config", str(config_path), "--task-name", task_name],
    )

    async def fail_with_unknown_exception(task_config, debug_limit):
        raise RuntimeError("api_key=cli-secret")

    monkeypatch.setattr(
        spider_v2, "scrape_xianyu", fail_with_unknown_exception
    )

    exit_code = asyncio.run(spider_v2.main())
    output = capsys.readouterr().out

    assert exit_code != 0
    assert "cli-secret" not in output
    assert "api_key=[REDACTED]" in output


@pytest.mark.parametrize("cancel_signal", [signal.SIGTERM, signal.SIGINT])
def test_cli_signal_cancellation_is_not_reported_as_failure(
    tmp_path,
    load_json_fixture,
    monkeypatch,
    capsys,
    cancel_signal,
):
    spider_v2 = _load_spider(monkeypatch)
    task_name, config_path = _write_keyword_task_config(
        tmp_path, load_json_fixture
    )
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(spider_v2, "STATE_FILE", str(state_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["spider_v2.py", "--config", str(config_path), "--task-name", task_name],
    )

    async def wait_until_cancelled(task_config, debug_limit):
        await asyncio.Event().wait()

    monkeypatch.setattr(spider_v2, "scrape_xianyu", wait_until_cancelled)

    async def run_with_signal():
        loop = asyncio.get_running_loop()
        handlers = {}
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, callback: handlers.__setitem__(sig, callback),
        )
        main_task = asyncio.create_task(spider_v2.main())
        while cancel_signal not in handlers:
            await asyncio.sleep(0)
        handlers[cancel_signal]()
        return await main_task

    exit_code = asyncio.run(run_with_signal())
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "用户取消" in output
    assert "风控终止" not in output
    assert "登录失效" not in output
