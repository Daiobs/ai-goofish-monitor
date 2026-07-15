import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import (
    build_legacy_result_filename,
    build_task_result_filename,
)
from src.keyword_rule_engine import build_search_text, normalize_text
from src.services import result_storage_service as result_service
from src.services.result_export_service import build_results_csv
from src.services.result_storage_service import (
    build_task_result_ndjson,
    load_all_task_result_records,
    load_task_result_summary,
    load_visible_task_result_item_ids,
    query_result_records,
    query_task_result_records,
    save_result_record,
    save_task_result_blacklist_keywords,
    save_task_result_record,
    update_task_result_item_status,
)


DEFAULT_QUERY = {
    "ai_recommended_only": False,
    "keyword_recommended_only": False,
    "sort_by": "crawl_time",
    "sort_order": "desc",
    "page": 1,
    "limit": 20,
    "include_hidden": False,
}


def _record(
    item_id: str,
    *,
    title: str | None = None,
    crawl_time: str = "2026-07-14T10:00:00",
    price: int = 100,
    recommended: bool = False,
    source: str | None = None,
    keyword: str = "fictional camera",
    task_name: str = "fictional task",
) -> dict:
    return {
        "爬取时间": crawl_time,
        "搜索关键字": keyword,
        "任务名称": task_name,
        "商品信息": {
            "商品ID": item_id,
            "商品标题": title or f"Fictional camera {item_id}",
            "商品链接": f"https://example.invalid/item/{item_id}",
            "当前售价": str(price),
            "发布时间": crawl_time,
        },
        "ai_analysis": {
            "is_recommended": recommended,
            "analysis_source": source,
            "keyword_hit_count": 1 if recommended else 0,
        },
    }


def _query_task(task_id: int, **overrides):
    options = {**DEFAULT_QUERY, **overrides}
    return asyncio.run(query_task_result_records(task_id, **options))


def _insert_structured_rows(conn, rows: list[tuple[int | None, str, dict, str]]):
    payloads = []
    for task_id, filename, record, status in rows:
        item = record["商品信息"]
        analysis = record["ai_analysis"]
        payloads.append(
            (
                task_id,
                filename,
                record["搜索关键字"],
                record["任务名称"],
                record["爬取时间"],
                item["发布时间"],
                float(item["当前售价"]),
                item["当前售价"],
                item["商品ID"],
                item["商品标题"],
                item["商品链接"],
                f"item:{item['商品ID']}",
                "fictional seller",
                int(analysis["is_recommended"]),
                analysis["analysis_source"],
                int(analysis["keyword_hit_count"]),
                status,
                normalize_text(build_search_text(record)),
                json.dumps(record, ensure_ascii=False),
            )
        )
    conn.executemany(
        """
        INSERT INTO result_items (
            task_id, result_filename, keyword, task_name, crawl_time,
            publish_time, price, price_display, item_id, title, link,
            link_unique_key, seller_nickname, is_recommended,
            analysis_source, keyword_hit_count, status, search_text, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payloads,
    )


def _insert_raw_result_row(
    conn,
    *,
    task_id: int,
    item_id: str,
    crawl_time: str,
    raw_record: dict,
    search_text: str = "",
    is_recommended: bool = False,
    analysis_source: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO result_items (
            task_id, result_filename, keyword, task_name, crawl_time,
            item_id, title, link_unique_key, is_recommended, analysis_source,
            keyword_hit_count, status, search_text, raw_json
        ) VALUES (?, ?, 'fictional camera', 'fictional task', ?, ?, ?, ?, ?, ?,
                  0, 'active', ?, ?)
        """,
        (
            task_id,
            build_task_result_filename(task_id),
            crawl_time,
            item_id,
            f"Structured {item_id}",
            f"item:{item_id}",
            int(is_recommended),
            analysis_source,
            search_text,
            json.dumps(raw_record, ensure_ascii=False),
        ),
    )


def test_task_query_uses_sql_paging_and_parses_only_page_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bootstrap_sqlite_storage()
    base_time = datetime(2026, 1, 1)
    rows = []
    for index in range(5003):
        record = _record(
            f"item-{index:04d}",
            crawl_time=(base_time + timedelta(seconds=index)).isoformat(),
            price=index,
        )
        rows.append((101, build_task_result_filename(101), record, "active"))
    rows.append(
        (
            102,
            build_task_result_filename(102),
            _record("item-0000", task_name="isolated task"),
            "active",
        )
    )
    with sqlite_connection() as conn:
        _insert_structured_rows(conn, rows)
        conn.commit()

    original_parse = result_service._parse_raw_record
    parse_calls = 0

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(result_service, "_parse_raw_record", counted_parse)

    total, first_page = _query_task(101)
    assert total == 5003
    assert len(first_page) == 20
    assert parse_calls == 20
    first_ids = [item["商品信息"]["商品ID"] for item in first_page]
    assert first_ids == [f"item-{index:04d}" for index in range(5002, 4982, -1)]

    parse_calls = 0
    _, second_page = _query_task(101, page=2)
    assert len(second_page) == 20
    assert parse_calls == 20
    second_ids = [item["商品信息"]["商品ID"] for item in second_page]
    assert second_ids == [f"item-{index:04d}" for index in range(4982, 4962, -1)]
    assert set(first_ids).isdisjoint(second_ids)

    _, last_page = _query_task(101, page=251)
    assert [item["商品信息"]["商品ID"] for item in last_page] == [
        "item-0002",
        "item-0001",
        "item-0000",
    ]
    total, beyond_end = _query_task(101, page=252)
    assert total == 5003
    assert beyond_end == []

    _, cheapest = _query_task(
        101,
        sort_by="price",
        sort_order="asc",
        limit=3,
    )
    assert [item["商品信息"]["商品ID"] for item in cheapest] == [
        "item-0000",
        "item-0001",
        "item-0002",
    ]
    isolated_total, isolated = _query_task(102)
    assert isolated_total == 1
    assert isolated[0]["任务名称"] == "isolated task"


def test_blacklist_and_status_filters_run_before_count_and_page(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    records = (
        _record("unicode", title="限量版 相机"),
        _record("ascii", title="Phone pro case"),
        _record("boundary", title="Profile camera"),
        _record("regex", title="Special   Edition camera"),
        _record("ordinary", title="Ordinary camera"),
        _record("manual", title="Manual hidden camera"),
        _record("expired", title="Expired camera"),
    )
    for record in records:
        assert asyncio.run(save_task_result_record(record, "fictional camera", 201))
    assert asyncio.run(update_task_result_item_status(201, "manual", "hidden"))
    assert asyncio.run(update_task_result_item_status(201, "expired", "expired"))
    asyncio.run(
        save_task_result_blacklist_keywords(
            201,
            ["限量版", "pro", r"re:special\s+edition", "re:["],
        )
    )

    total, visible = _query_task(201, limit=100)
    assert total == 2
    assert {item["商品信息"]["商品ID"] for item in visible} == {
        "boundary",
        "ordinary",
    }

    all_total, all_items = _query_task(201, include_hidden=True, limit=100)
    assert all_total == 7
    by_id = {item["商品信息"]["商品ID"]: item for item in all_items}
    assert by_id["unicode"]["_hidden_reason"] == "rule"
    assert by_id["ascii"]["_matched_blacklist_keywords"] == ["pro"]
    assert by_id["boundary"]["_effective_hidden"] is False
    assert by_id["regex"]["_hidden_reason"] == "rule"
    assert by_id["manual"]["_hidden_reason"] == "manual"
    assert by_id["expired"]["_hidden_reason"] == "expired"

    asyncio.run(save_task_result_blacklist_keywords(201, ["ordinary"]))
    total, visible = _query_task(201, limit=100)
    assert total == 4
    assert "ordinary" not in {
        item["商品信息"]["商品ID"] for item in visible
    }


def test_task_and_escaped_legacy_namespaces_remain_isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert asyncio.run(
        save_task_result_record(
            _record("task-owned", keyword="task_42", task_name="task owned"),
            "task_42",
            42,
        )
    )
    assert asyncio.run(
        save_result_record(
            _record("legacy", keyword="task_42", task_name="legacy config"),
            "task_42",
        )
    )

    task_total, task_items = asyncio.run(
        query_result_records(build_task_result_filename(42), **DEFAULT_QUERY)
    )
    legacy_total, legacy_items = asyncio.run(
        query_result_records(build_legacy_result_filename("task_42"), **DEFAULT_QUERY)
    )
    assert task_total == legacy_total == 1
    assert task_items[0]["商品信息"]["商品ID"] == "task-owned"
    assert legacy_items[0]["商品信息"]["商品ID"] == "legacy"


def test_count_and_page_share_one_read_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert asyncio.run(
        save_task_result_record(_record("before"), "fictional camera", 250)
    )
    inserted = False

    class ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, sql, parameters=()):
            nonlocal inserted
            cursor = self._connection.execute(sql, parameters)
            if "SELECT COUNT(*) AS total" in sql and not inserted:
                inserted = True
                record = _record(
                    "during-query",
                    crawl_time="2026-07-14T11:00:00",
                )
                with sqlite_connection() as writer:
                    _insert_structured_rows(
                        writer,
                        [
                            (
                                250,
                                build_task_result_filename(250),
                                record,
                                "active",
                            )
                        ],
                    )
                    writer.commit()
            return cursor

    @contextmanager
    def proxied_connection(*args, **kwargs):
        with sqlite_connection(*args, **kwargs) as connection:
            yield ConnectionProxy(connection)

    monkeypatch.setattr(result_service, "sqlite_connection", proxied_connection)
    total, items = _query_task(250)
    assert total == 1
    assert [item["商品信息"]["商品ID"] for item in items] == ["before"]

    monkeypatch.setattr(result_service, "sqlite_connection", sqlite_connection)
    total, items = _query_task(250)
    assert total == 2
    assert {item["商品信息"]["商品ID"] for item in items} == {
        "before",
        "during-query",
    }


def test_summary_and_visible_ids_use_structured_sql_without_full_load(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    records = (
        _record(
            "ai-latest",
            crawl_time="2026-07-14T12:00:00",
            recommended=True,
            source="ai",
        ),
        _record(
            "keyword-older",
            crawl_time="2026-07-14T11:00:00",
            recommended=True,
            source="keyword",
        ),
        _record("plain", crawl_time="2026-07-14T10:00:00"),
        _record("rule-hidden", title="Blocked camera", crawl_time="2026-07-14T13:00:00"),
        _record("manual-hidden", crawl_time="2026-07-14T14:00:00"),
    )
    for record in records:
        assert asyncio.run(save_task_result_record(record, "fictional camera", 301))
    asyncio.run(save_task_result_blacklist_keywords(301, ["blocked"]))
    asyncio.run(update_task_result_item_status(301, "manual-hidden", "hidden"))

    monkeypatch.setattr(
        result_service,
        "_load_all_records_sync",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("summary and visible IDs must not load all records")
        ),
    )
    original_parse = result_service._parse_raw_record
    parse_calls = 0

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(result_service, "_parse_raw_record", counted_parse)
    summary = asyncio.run(load_task_result_summary(301))
    assert summary["total_items"] == 3
    assert summary["recommended_items"] == 2
    assert summary["ai_recommended_items"] == 1
    assert summary["keyword_recommended_items"] == 1
    assert summary["latest_crawl_time"] == "2026-07-14T12:00:00"
    assert summary["latest_record"]["商品信息"]["商品ID"] == "ai-latest"
    assert summary["latest_recommendation"]["商品信息"]["商品ID"] == "ai-latest"
    assert parse_calls == 2

    monkeypatch.setattr(
        result_service,
        "_parse_raw_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("visible IDs must not parse raw JSON")
        ),
    )
    assert load_visible_task_result_item_ids(301) == {
        "ai-latest",
        "keyword-older",
        "plain",
    }


def test_corrupt_page_row_counts_but_is_skipped_without_api_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bootstrap_sqlite_storage()
    good = _record("good", crawl_time="2026-07-14T10:00:00")
    with sqlite_connection() as conn:
        _insert_structured_rows(
            conn,
            [(401, build_task_result_filename(401), good, "active")],
        )
        conn.execute(
            """
            INSERT INTO result_items (
                task_id, result_filename, keyword, task_name, crawl_time,
                item_id, link_unique_key, is_recommended, keyword_hit_count,
                status, search_text, raw_json
            ) VALUES (401, ?, 'fictional camera', 'fictional task',
                      '2026-07-14T11:00:00', 'broken', 'item:broken',
                      0, 0, 'active', '', '{broken json')
            """,
            (build_task_result_filename(401),),
        )
        conn.commit()

    total, items = _query_task(401)
    assert total == 2
    assert [item["商品信息"]["商品ID"] for item in items] == ["good"]
    summary = asyncio.run(load_task_result_summary(401))
    assert summary["total_items"] == 2
    assert summary["latest_crawl_time"] == "2026-07-14T11:00:00"
    assert summary["latest_record"] is None


def test_structurally_malformed_rows_are_counted_but_skipped_safely(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    bootstrap_sqlite_storage()
    malformed_records = [
        {"商品信息": []},
        {"商品信息": "invalid"},
        {"ai_analysis": []},
        {"卖家信息": []},
    ]
    with sqlite_connection() as conn:
        for index, raw_record in enumerate(malformed_records):
            _insert_raw_result_row(
                conn,
                task_id=402,
                item_id=f"malformed-{index}",
                crawl_time=f"2026-07-14T1{index}:00:00",
                raw_record=raw_record,
                is_recommended=index == len(malformed_records) - 1,
                analysis_source="ai" if index == len(malformed_records) - 1 else None,
            )
        conn.commit()

    for include_hidden in (False, True):
        total, items = _query_task(
            402,
            include_hidden=include_hidden,
            limit=100,
        )
        assert total == 4
        assert items == []
        all_records = asyncio.run(
            load_all_task_result_records(
                402,
                ai_recommended_only=False,
                keyword_recommended_only=False,
                sort_by="crawl_time",
                sort_order="desc",
                include_hidden=include_hidden,
            )
        )
        assert all_records == []
        assert build_results_csv(all_records).startswith("任务名称,搜索关键字")

    summary = asyncio.run(load_task_result_summary(402))
    assert summary["total_items"] == 4
    assert summary["recommended_items"] == 1
    assert summary["latest_record"] is None
    assert summary["latest_recommendation"] is None
    assert load_visible_task_result_item_ids(402) == {
        "malformed-0",
        "malformed-1",
        "malformed-2",
        "malformed-3",
    }
    ndjson_records = [
        json.loads(line)
        for line in asyncio.run(build_task_result_ndjson(402)).splitlines()
    ]
    assert ndjson_records == malformed_records


def test_visibility_decoration_uses_persisted_search_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        _insert_raw_result_row(
            conn,
            task_id=403,
            item_id="persisted-match",
            crawl_time="2026-07-14T10:00:00",
            raw_record=_record("persisted-match", title="Ordinary camera"),
            search_text="persisted blocked marker",
        )
        _insert_raw_result_row(
            conn,
            task_id=403,
            item_id="raw-only-match",
            crawl_time="2026-07-14T11:00:00",
            raw_record=_record("raw-only-match", title="Blocked in raw JSON"),
            search_text="",
        )
        conn.commit()
    asyncio.run(save_task_result_blacklist_keywords(403, ["blocked"]))

    monkeypatch.setattr(
        result_service,
        "build_search_text",
        lambda _record: (_ for _ in ()).throw(
            AssertionError("reads must use persisted search_text")
        ),
    )

    visible_total, visible = _query_task(403, limit=100)
    assert visible_total == 1
    assert visible[0]["商品信息"]["商品ID"] == "raw-only-match"
    assert visible[0]["_matched_blacklist_keywords"] == []
    assert visible[0]["_effective_hidden"] is False

    all_total, all_items = _query_task(403, include_hidden=True, limit=100)
    assert all_total == 2
    by_id = {item["商品信息"]["商品ID"]: item for item in all_items}
    assert by_id["persisted-match"]["_matched_blacklist_keywords"] == ["blocked"]
    assert by_id["persisted-match"]["_hidden_reason"] == "rule"
    assert by_id["raw-only-match"]["_matched_blacklist_keywords"] == []

    loaded_items = asyncio.run(
        load_all_task_result_records(
            403,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=True,
        )
    )
    loaded_by_id = {
        item["商品信息"]["商品ID"]: item for item in loaded_items
    }
    assert loaded_by_id["persisted-match"]["_matched_blacklist_keywords"] == [
        "blocked"
    ]
    assert loaded_by_id["raw-only-match"]["_matched_blacklist_keywords"] == []

    summary = asyncio.run(load_task_result_summary(403))
    assert summary["latest_record"]["商品信息"]["商品ID"] == "raw-only-match"
    assert summary["latest_record"]["_matched_blacklist_keywords"] == []


def test_huge_page_short_circuits_before_sqlite_offset_binding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert asyncio.run(save_task_result_record(_record("only"), "fictional camera", 404))
    page_selects = 0

    class ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, sql, parameters=()):
            nonlocal page_selects
            if "LIMIT ? OFFSET ?" in sql:
                page_selects += 1
            return self._connection.execute(sql, parameters)

    @contextmanager
    def proxied_connection(*args, **kwargs):
        with sqlite_connection(*args, **kwargs) as connection:
            yield ConnectionProxy(connection)

    monkeypatch.setattr(result_service, "sqlite_connection", proxied_connection)

    total, items = _query_task(404, page=10**19)
    assert total == 1
    assert items == []
    assert page_selects == 0

    total, items = _query_task(404, limit=0)
    assert total == 1
    assert items == []
    assert page_selects == 0
