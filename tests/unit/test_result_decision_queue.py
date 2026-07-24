import asyncio

import pytest

from src.services.result_storage_service import (
    query_task_decision_records,
    save_task_result_record,
)


_MISSING = object()


@pytest.fixture()
def decision_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))


def _record(
    item_id: str,
    *,
    category: str,
    comparable: bool,
    status: str = "completed",
    recommended: bool = False,
    value_score=_MISSING,
    price: float = 100,
    crawl_time: str = "2026-07-24T10:00:00",
) -> dict:
    analysis = {
        "analysis_source": "ai",
        "analysis_status": status,
        "is_recommended": recommended,
        "target_category": category,
        "market_comparable": comparable,
        "reason": f"decision for {item_id}",
    }
    if value_score is not _MISSING:
        analysis["value_score"] = value_score
    return {
        "爬取时间": crawl_time,
        "搜索关键字": "charger",
        "任务名称": "Decision queue",
        "商品信息": {
            "商品ID": item_id,
            "商品标题": f"Item {item_id}",
            "商品链接": f"https://www.goofish.com/item?id={item_id}",
            "当前售价": str(price),
        },
        "ai_analysis": analysis,
    }


def _save(task_id: int, record: dict) -> None:
    assert asyncio.run(
        save_task_result_record(record, "charger", task_id)
    )


def _query(
    task_id: int,
    decision_view: str,
    *,
    page: int = 1,
    limit: int = 100,
):
    return asyncio.run(
        query_task_decision_records(
            task_id,
            decision_view=decision_view,
            page=page,
            limit=limit,
        )
    )


def _item_ids(records: list[dict]) -> list[str]:
    return [str(record["商品信息"]["商品ID"]) for record in records]


def test_decision_views_filter_full_task_and_report_distinct_summary_counts(
    decision_db,
):
    records = [
        _record(
            "worth",
            category="target_only",
            comparable=True,
            recommended=True,
        ),
        _record(
            "comparable-no",
            category="target_only",
            comparable=True,
        ),
        _record(
            "bundle",
            category="target_bundle",
            comparable=False,
        ),
        _record(
            "not-target",
            category="not_target",
            comparable=False,
        ),
        _record(
            "uncertain",
            category="uncertain",
            comparable=False,
        ),
        _record(
            "failed",
            category="target_only",
            comparable=False,
            status="failed",
        ),
        _record(
            "skipped",
            category="target_only",
            comparable=True,
            status="skipped",
        ),
        _record(
            "pending",
            category="target_only",
            comparable=False,
            status="pending",
        ),
    ]
    for record in records:
        _save(101, record)

    expected_views = {
        "worth_viewing": {"worth"},
        "comparable_targets": {"worth", "comparable-no"},
        "bundles": {"bundle"},
        "excluded": {
            "bundle",
            "not-target",
            "uncertain",
            "failed",
            "pending",
        },
        "ai_issues": {"failed", "skipped", "pending"},
    }
    expected_summary = {
        "all_count": 8,
        "target_only_count": 5,
        "target_bundle_count": 1,
        "not_target_count": 1,
        "uncertain_count": 1,
        "comparable_count": 2,
        "excluded_count": 5,
        "ai_recommended_count": 1,
        "ai_not_recommended_count": 4,
        "ai_issue_count": 3,
    }

    for view, expected_ids in expected_views.items():
        total, items, summary = _query(101, view)
        assert total == len(expected_ids)
        assert set(_item_ids(items)) == expected_ids
        assert summary == expected_summary


def test_worth_viewing_filter_runs_before_pagination_across_entire_task(
    decision_db,
):
    for index in range(30):
        _save(
            101,
            _record(
                f"excluded-{index:02d}",
                category="not_target",
                comparable=False,
                crawl_time=f"2026-07-24T12:{index:02d}:00",
            ),
        )
    _save(
        101,
        _record(
            "older-worth",
            category="target_only",
            comparable=True,
            recommended=True,
            crawl_time="2026-07-23T10:00:00",
        ),
    )

    total, items, summary = _query(
        101,
        "worth_viewing",
        page=1,
        limit=1,
    )

    assert total == 1
    assert _item_ids(items) == ["older-worth"]
    assert summary["all_count"] == 31
    assert summary["excluded_count"] == 30


def test_decision_order_is_deterministic_without_fabricating_value_score(
    decision_db,
):
    ordered_records = [
        _record(
            "rec-scored",
            category="target_only",
            comparable=True,
            recommended=True,
            value_score=70,
            price=100,
            crawl_time="2026-07-20T10:00:00",
        ),
        _record(
            "rec-missing-below",
            category="target_only",
            comparable=True,
            recommended=True,
            price=10,
            crawl_time="2026-07-19T10:00:00",
        ),
        _record(
            "rec-missing-above",
            category="target_only",
            comparable=True,
            recommended=True,
            price=200,
            crawl_time="2026-07-24T10:00:00",
        ),
        _record(
            "no-scored",
            category="target_only",
            comparable=True,
            value_score=99,
            price=150,
            crawl_time="2026-07-24T10:00:00",
        ),
        _record(
            "no-missing-below",
            category="target_only",
            comparable=True,
            price=20,
            crawl_time="2026-07-18T10:00:00",
        ),
        _record(
            "no-missing-above",
            category="target_only",
            comparable=True,
            price=300,
            crawl_time="2026-07-24T10:00:00",
        ),
    ]
    for record in ordered_records:
        _save(101, record)

    total, items, _ = _query(101, "comparable_targets")

    assert total == 6
    assert _item_ids(items) == [
        "rec-scored",
        "rec-missing-below",
        "rec-missing-above",
        "no-scored",
        "no-missing-below",
        "no-missing-above",
    ]
    missing = next(
        item for item in items
        if item["商品信息"]["商品ID"] == "rec-missing-below"
    )
    assert "value_score" not in missing["ai_analysis"]


def test_decision_pagination_is_stable_for_tied_rows(decision_db):
    for item_id in ("tie-c", "tie-a", "tie-d", "tie-b"):
        _save(
            101,
            _record(
                item_id,
                category="target_only",
                comparable=True,
                recommended=True,
                value_score=80,
                price=100,
                crawl_time="2026-07-24T10:00:00",
            ),
        )

    first = _query(101, "worth_viewing", page=1, limit=2)
    second = _query(101, "worth_viewing", page=2, limit=2)
    repeated = _query(101, "worth_viewing", page=1, limit=2)

    assert first[0] == second[0] == 4
    assert _item_ids(first[1]) == ["tie-a", "tie-b"]
    assert _item_ids(second[1]) == ["tie-c", "tie-d"]
    assert _item_ids(repeated[1]) == _item_ids(first[1])
    assert set(_item_ids(first[1])).isdisjoint(_item_ids(second[1]))


def test_decision_query_keeps_task_ownership_isolated(decision_db):
    _save(
        101,
        _record(
            "task-a",
            category="target_only",
            comparable=True,
            recommended=True,
        ),
    )
    for item_id in ("task-b-1", "task-b-2"):
        _save(
            102,
            _record(
                item_id,
                category="target_only",
                comparable=True,
                recommended=True,
            ),
        )

    total_a, items_a, summary_a = _query(101, "worth_viewing")
    total_b, items_b, summary_b = _query(102, "worth_viewing")

    assert total_a == summary_a["all_count"] == 1
    assert _item_ids(items_a) == ["task-a"]
    assert total_b == summary_b["all_count"] == 2
    assert set(_item_ids(items_b)) == {"task-b-1", "task-b-2"}


def test_decision_query_rejects_unknown_view(decision_db):
    with pytest.raises(ValueError, match="unsupported decision view"):
        _query(101, "not-a-view")


def test_completed_record_without_boolean_decision_is_not_not_recommended(
    decision_db,
):
    record = _record(
        "missing-decision",
        category="target_only",
        comparable=True,
    )
    record["ai_analysis"]["is_recommended"] = None
    _save(101, record)

    _, _, summary = _query(101, "comparable_targets")

    assert summary["ai_recommended_count"] == 0
    assert summary["ai_not_recommended_count"] == 0
