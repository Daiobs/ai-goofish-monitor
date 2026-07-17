import asyncio

import pytest

from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import (
    build_task_result_filename,
    parse_task_result_filename,
    try_parse_task_result_filename,
)
from src.services.result_storage_service import (
    build_result_ndjson,
    build_task_result_ndjson,
    delete_task_result_records,
    list_result_filenames,
    load_all_result_records,
    load_all_task_result_records,
    load_processed_link_keys,
    load_task_processed_link_keys,
    load_task_result_blacklist_keywords,
    save_result_blacklist_keywords,
    save_result_record,
    save_task_result_blacklist_keywords,
    save_task_result_record,
    upsert_task_result_record,
    update_task_result_item_status,
)


def _record(*, item_id: str, task_name: str, keyword: str = "camera") -> dict:
    return {
        "爬取时间": "2026-07-14T08:00:00",
        "搜索关键字": keyword,
        "任务名称": task_name,
        "商品信息": {
            "商品ID": item_id,
            "商品标题": f"Camera {item_id}",
            "商品链接": f"https://www.goofish.com/item?id={item_id}&from=test",
            "当前售价": "1000",
        },
        "ai_analysis": {
            "analysis_source": "keyword",
            "is_recommended": True,
            "keyword_hit_count": 1,
        },
    }


def _load_task_records(task_id: int, *, include_hidden: bool = True) -> list[dict]:
    return asyncio.run(
        load_all_task_result_records(
            task_id,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=include_hidden,
        )
    )


def _load_legacy_records(filename: str) -> list[dict]:
    return asyncio.run(
        load_all_result_records(
            filename,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=True,
        )
    )


def test_same_keyword_tasks_keep_results_dedupe_and_legacy_data_isolated(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)

    assert asyncio.run(save_task_result_record(_record(item_id="same", task_name="A"), "camera", 11))
    assert asyncio.run(save_task_result_record(_record(item_id="same", task_name="B"), "camera", 12))
    assert asyncio.run(save_result_record(_record(item_id="same", task_name="Legacy"), "camera"))

    assert load_task_processed_link_keys(11) == {"https://www.goofish.com/item?id=same"}
    assert load_task_processed_link_keys(12) == {"https://www.goofish.com/item?id=same"}
    assert load_processed_link_keys("camera") == {"https://www.goofish.com/item?id=same"}

    task_a = _load_task_records(11)
    task_b = _load_task_records(12)
    legacy = _load_legacy_records("camera_full_data.jsonl")
    assert [record["任务名称"] for record in task_a] == ["A"]
    assert [record["任务名称"] for record in task_b] == ["B"]
    assert [record["任务名称"] for record in legacy] == ["Legacy"]
    assert task_a[0]["任务ID"] == 11
    assert task_b[0]["任务ID"] == 12
    assert "任务ID" not in legacy[0]

    filenames = asyncio.run(list_result_filenames())
    assert set(filenames) == {
        "task_11_full_data.jsonl",
        "task_12_full_data.jsonl",
        "camera_full_data.jsonl",
    }
    assert "\"任务ID\": 11" in asyncio.run(build_task_result_ndjson(11))
    assert "任务ID" not in asyncio.run(build_result_ndjson("camera_full_data.jsonl"))


def test_task_blacklist_and_manual_status_are_task_scoped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(save_task_result_record(_record(item_id="same", task_name="A"), "camera", 21))
    asyncio.run(save_task_result_record(_record(item_id="same", task_name="B"), "camera", 22))

    asyncio.run(save_task_result_blacklist_keywords(21, ["Camera same"]))
    asyncio.run(save_task_result_blacklist_keywords(22, ["unrelated"]))
    asyncio.run(save_result_blacklist_keywords("camera_full_data.jsonl", ["legacy-only"]))

    assert asyncio.run(load_task_result_blacklist_keywords(21)) == ["camera same"]
    assert asyncio.run(load_task_result_blacklist_keywords(22)) == ["unrelated"]
    assert _load_task_records(21, include_hidden=False) == []
    assert len(_load_task_records(22, include_hidden=False)) == 1

    assert asyncio.run(update_task_result_item_status(22, "same", "hidden")) is True
    assert _load_task_records(22, include_hidden=False) == []
    assert _load_task_records(21, include_hidden=True)[0]["_status"] == "active"


def test_task_deletion_preserves_other_task_and_legacy_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(save_task_result_record(_record(item_id="same", task_name="A"), "camera", 31))
    asyncio.run(save_task_result_record(_record(item_id="same", task_name="B"), "camera", 32))
    asyncio.run(save_result_record(_record(item_id="same", task_name="Legacy"), "camera"))

    assert asyncio.run(delete_task_result_records(31)) == 1
    assert _load_task_records(31) == []
    assert len(_load_task_records(32)) == 1
    assert len(_load_legacy_records("camera_full_data.jsonl")) == 1


def test_task_result_filename_parser_is_strict():
    assert build_task_result_filename(42) == "task_42_full_data.jsonl"
    assert parse_task_result_filename("task_42_full_data.jsonl") == 42
    assert try_parse_task_result_filename("task_camera_full_data.jsonl") is None

    for invalid in (
        "task_01_full_data.jsonl",
        "task_-1_full_data.jsonl",
        "task_1.jsonl",
        "../task_1_full_data.jsonl",
        "task_1_full_data.jsonl/extra",
    ):
        with pytest.raises(ValueError):
            parse_task_result_filename(invalid)


def test_task_identity_survives_task_display_field_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(
        save_task_result_record(
            _record(item_id="stable", task_name="Before", keyword="old keyword"),
            "old keyword",
            44,
        )
    )

    with sqlite_connection() as conn:
        conn.execute(
            "UPDATE result_items SET task_name = ?, keyword = ? WHERE task_id = ?",
            ("Historical snapshot", "historical keyword", 44),
        )
        conn.commit()

    records = _load_task_records(44)
    assert len(records) == 1
    assert records[0]["商品信息"]["商品ID"] == "stable"


def test_task_result_final_update_persists_canonical_prompt_version(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    pending = _record(item_id="ai-item", task_name="AI Task")
    pending["ai_analysis"] = {
        "analysis_source": "ai",
        "analysis_status": "pending",
        "is_recommended": None,
        "reason": "",
        "keyword_hit_count": 0,
    }
    assert asyncio.run(save_task_result_record(pending, "camera", 45)) is True

    completed = _record(item_id="ai-item", task_name="AI Task")
    completed["ai_analysis"] = {
        "analysis_source": "ai",
        "analysis_status": "completed",
        "prompt_version": "EagleEye-V6.4",
        "is_recommended": True,
        "reason": "meets the fictional criteria",
        "risk_tags": [],
        "criteria_analysis": {"seller_type": "个人"},
        "request_duration_seconds": 1.234,
        "keyword_hit_count": 0,
    }

    assert asyncio.run(
        upsert_task_result_record(completed, "camera", 45)
    ) is True
    records = _load_task_records(45)

    assert len(records) == 1
    assert records[0]["ai_analysis"] == completed["ai_analysis"]
