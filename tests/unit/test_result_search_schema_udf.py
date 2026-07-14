from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from src.infrastructure.persistence import sqlite_connection as connection_module
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import (
    PAGINATION_INDEXES,
    RESULT_SEARCH_TEXT_MIGRATION_KEY,
    init_schema,
    sqlite_connection,
)
from src.keyword_rule_engine import build_search_text, normalize_text
from src.services import result_storage_service as result_service
from src.services.result_blacklist_service import (
    match_blacklist_keywords,
    match_blacklist_search_text,
)
from src.services.result_storage_service import save_task_result_record


DATA_01A_UNIQUE_INDEXES = {
    "idx_result_items_task_link_unique",
    "idx_result_items_legacy_file_link_unique",
    "idx_price_snapshots_task_run_item_unique",
    "idx_price_snapshots_legacy_run_item_unique",
}


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_pre_search_database(path, records: list[tuple[int, str]]) -> sqlite3.Connection:
    conn = _connect(path)
    conn.execute(
        """
        CREATE TABLE result_items (
            id INTEGER PRIMARY KEY,
            result_filename TEXT NOT NULL,
            keyword TEXT NOT NULL,
            task_name TEXT NOT NULL,
            crawl_time TEXT NOT NULL,
            publish_time TEXT,
            price REAL,
            price_display TEXT,
            item_id TEXT,
            title TEXT,
            link TEXT,
            link_unique_key TEXT NOT NULL,
            seller_nickname TEXT,
            is_recommended INTEGER NOT NULL,
            analysis_source TEXT,
            keyword_hit_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            raw_json TEXT NOT NULL,
            UNIQUE(result_filename, link_unique_key)
        )
        """
    )
    for row_id, raw_json in records:
        conn.execute(
            """
            INSERT INTO result_items (
                id, result_filename, keyword, task_name, crawl_time,
                item_id, link_unique_key, is_recommended,
                keyword_hit_count, raw_json
            ) VALUES (?, 'fiction_full_data.jsonl', 'fiction', '', ?, ?, ?, 0, 0, ?)
            """,
            (
                row_id,
                f"2026-01-{row_id:02d}T00:00:00",
                f"item-{row_id}",
                f"link-{row_id}",
                raw_json,
            ),
        )
    conn.commit()
    return conn


def _valid_record() -> dict:
    return {
        "搜索关键字": "fiction",
        "任务名称": "fixture-task",
        "爬取时间": "2026-01-01T00:00:00",
        "商品信息": {
            "商品ID": "fixture-item",
            "商品标题": "虚构 Q1 限定版",
            "商品描述": "仅用于离线测试",
            "商品链接": "https://invalid.example/item?id=fixture",
            "当前售价": "88",
        },
        "卖家信息": {"卖家昵称": "示例卖家"},
        "ai_analysis": {
            "is_recommended": True,
            "analysis_source": "keyword",
            "keyword_hit_count": 1,
        },
    }


def _plan(conn: sqlite3.Connection, sql: str, params: tuple) -> str:
    return "\n".join(
        str(row["detail"])
        for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    )


def _production_page_plan(
    conn: sqlite3.Connection,
    *,
    task_id: int | None = None,
    filename: str | None = None,
    ai_recommended_only: bool = False,
    sort_by: str = "crawl_time",
    sort_order: str = "desc",
) -> str:
    where_clause, params, _, _ = result_service._prepare_filtered_query(
        conn,
        filename=filename,
        task_id=task_id,
        ai_recommended_only=ai_recommended_only,
        keyword_recommended_only=False,
        include_hidden=False,
    )
    order_by = result_service._sort_expression(
        sort_by,
        sort_order,
        include_hidden=False,
    )
    sql = (
        "SELECT raw_json, status FROM result_items "
        f"WHERE {where_clause} ORDER BY {order_by} LIMIT ? OFFSET ?"
    )
    return _plan(conn, sql, (*params, 20, 0))


def _index_key_columns(
    conn: sqlite3.Connection,
    index_name: str,
) -> list[tuple[str | None, int]]:
    escaped = index_name.replace('"', '""')
    return [
        (
            str(row["name"]) if row["name"] is not None else None,
            int(row["desc"]),
        )
        for row in conn.execute(f'PRAGMA index_xinfo("{escaped}")').fetchall()
        if int(row["key"]) == 1
    ]


def test_search_text_migration_backfills_bad_json_and_is_healthy_idempotent(tmp_path):
    valid_raw = json.dumps(_valid_record(), ensure_ascii=False, indent=2)
    invalid_raw = '{"商品信息":'
    invalid_shape_raw = '{"商品信息": []}'
    conn = _create_pre_search_database(
        tmp_path / "app.sqlite3",
        [(7, valid_raw), (8, invalid_raw), (9, invalid_shape_raw)],
    )

    init_schema(conn)

    columns = {
        str(row["name"]): row
        for row in conn.execute("PRAGMA table_info(result_items)").fetchall()
    }
    rows = conn.execute(
        "SELECT id, search_text, raw_json FROM result_items ORDER BY id"
    ).fetchall()
    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (RESULT_SEARCH_TEXT_MIGRATION_KEY,),
    ).fetchone()
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'result_items'"
    ).fetchone()["sql"]

    assert columns["search_text"]["type"] == "TEXT"
    assert columns["search_text"]["notnull"] == 1
    assert columns["search_text"]["dflt_value"] == "''"
    assert "AUTOINCREMENT" in str(table_sql).upper()
    assert rows[0]["search_text"] == normalize_text(
        build_search_text(json.loads(valid_raw))
    )
    assert rows[1]["search_text"] == ""
    assert rows[2]["search_text"] == ""
    assert [row["raw_json"] for row in rows] == [
        valid_raw,
        invalid_raw,
        invalid_shape_raw,
    ]
    assert json.loads(marker["value"]) == {"invalid_json": 2, "rows": 3}

    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    init_schema(conn)
    conn.set_trace_callback(None)

    normalized = [statement.strip().upper() for statement in traced]
    assert normalized
    assert not any(statement.startswith("BEGIN") for statement in normalized)
    assert all(statement.startswith(("SELECT ", "PRAGMA ")) for statement in normalized)
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT id, search_text, raw_json FROM result_items ORDER BY id"
        ).fetchall()
    ] == [tuple(row) for row in rows]
    conn.close()


def test_search_text_migration_failure_rolls_back_rebuild_and_data(tmp_path, monkeypatch):
    raw_json = json.dumps(_valid_record(), ensure_ascii=False)
    conn = _create_pre_search_database(tmp_path / "app.sqlite3", [(5, raw_json)])

    def fail_build_search_text(_record):
        raise RuntimeError("search backfill failed")

    monkeypatch.setattr(
        connection_module,
        "build_search_text",
        fail_build_search_text,
    )

    with pytest.raises(RuntimeError, match="search backfill failed"):
        init_schema(conn)

    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(result_items)").fetchall()
    }
    raw_after = conn.execute("SELECT raw_json FROM result_items").fetchone()[0]
    app_metadata = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_metadata'"
    ).fetchone()
    temp_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'result_items__task_owner_migration'"
    ).fetchone()

    assert "task_id" not in columns
    assert "search_text" not in columns
    assert raw_after == raw_json
    assert app_metadata is None
    assert temp_table is None
    assert conn.in_transaction is False
    conn.close()


def test_task_owner_rebuild_preserves_existing_search_text(tmp_path):
    raw_json = json.dumps(_valid_record(), ensure_ascii=False)
    conn = _create_pre_search_database(tmp_path / "app.sqlite3", [(15, raw_json)])
    conn.execute(
        "ALTER TABLE result_items "
        "ADD COLUMN search_text TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "UPDATE result_items SET search_text = 'preserved search sentinel'"
    )
    conn.execute(
        "CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO app_metadata(key, value) VALUES (?, 'already-backfilled')",
        (RESULT_SEARCH_TEXT_MIGRATION_KEY,),
    )
    conn.commit()

    init_schema(conn)

    row = conn.execute(
        "SELECT search_text, raw_json FROM result_items WHERE id = 15"
    ).fetchone()
    assert row["search_text"] == "preserved search sentinel"
    assert row["raw_json"] == raw_json
    conn.close()


def test_blacklist_search_text_helper_and_sqlite_udf_semantics(tmp_path, monkeypatch):
    db_path = tmp_path / "app.sqlite3"
    rules_json = json.dumps(["限定版", "q1"], ensure_ascii=False)
    normalized = normalize_text("虚构 Q1R5 与 Pro-Max 限定版")
    expected = ["限定版"]

    assert match_blacklist_search_text(normalized, ["限定版", "q1"]) == expected
    assert match_blacklist_keywords(_valid_record(), ["限定版", "q1"]) == [
        "限定版",
        "q1",
    ]

    connection_module._parse_blacklist_rules_json.cache_clear()
    with sqlite_connection(str(db_path)) as conn:
        conn.execute("CREATE TABLE connection_probe (value TEXT)")
        conn.commit()

        def udf(search_text: str, encoded_rules: str) -> int:
            return int(
                conn.execute(
                    "SELECT result_blacklist_match(?, ?)",
                    (search_text, encoded_rules),
                ).fetchone()[0]
            )

        assert udf(normalized, rules_json) == 1
        assert udf(normalize_text("虚构 q1r5"), json.dumps(["q1"])) == 0
        assert udf(normalize_text("虚构 q1-pro"), json.dumps(["q1"])) == 1
        assert udf(
            normalize_text("虚构 PRO-max 版本"),
            json.dumps([r"re:\b(pm|pro[\s-]?max)\b"]),
        ) == 1
        assert udf(normalized, json.dumps(["re:["])) == 0
        assert udf(normalized, "[]") == 0
        assert udf(normalized, "not-json") == 1
        assert udf(normalize_text("另一个限定版"), rules_json) == 1

        cache_info = connection_module._parse_blacklist_rules_json.cache_info()
        assert cache_info.maxsize == 128
        assert cache_info.hits >= 1
        function_row = conn.execute(
            "SELECT flags FROM pragma_function_list "
            "WHERE name = 'result_blacklist_match' AND narg = 2"
        ).fetchone()
        assert function_row is not None
        assert int(function_row["flags"]) & 0x800

    with sqlite_connection(str(db_path), read_only=True) as conn:
        assert conn.execute(
            "SELECT result_blacklist_match(?, ?)",
            (normalize_text("只读限定版"), rules_json),
        ).fetchone()[0] == 1

    def fail_match(_search_text, _keywords):
        raise RuntimeError("unexpected matcher failure")

    monkeypatch.setattr(
        connection_module,
        "match_blacklist_search_text",
        fail_match,
    )
    with sqlite_connection(str(db_path)) as conn:
        assert conn.execute(
            "SELECT result_blacklist_match(?, ?)",
            (normalized, rules_json),
        ).fetchone()[0] == 1


def test_legacy_jsonl_import_writes_normalized_search_text(tmp_path):
    record = _valid_record()
    legacy_dir = tmp_path / "legacy_results"
    legacy_dir.mkdir()
    (legacy_dir / "fiction_full_data.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "app.sqlite3"

    bootstrap_sqlite_storage(
        str(db_path),
        legacy_config_file=None,
        legacy_result_dir=str(legacy_dir),
        legacy_price_history_dir=str(tmp_path / "missing_history"),
    )

    with sqlite_connection(str(db_path), read_only=True) as conn:
        row = conn.execute(
            "SELECT search_text, raw_json FROM result_items WHERE item_id = ?",
            ("fixture-item",),
        ).fetchone()
    assert row is not None
    assert row["search_text"] == normalize_text(build_search_text(record))
    assert json.loads(row["raw_json"]) == record


def test_task_result_write_helper_stores_normalized_search_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record = _valid_record()

    assert asyncio.run(save_task_result_record(record, "fiction", 73)) is True

    with sqlite_connection(read_only=True) as conn:
        row = conn.execute(
            """
            SELECT task_id, result_filename, keyword, task_name, crawl_time,
                   publish_time, price, item_id, title, link, seller_nickname,
                   is_recommended, analysis_source, keyword_hit_count, status,
                   search_text, raw_json
            FROM result_items
            WHERE item_id = ?
            """,
            ("fixture-item",),
        ).fetchone()
    assert row is not None
    assert row["task_id"] == 73
    assert row["result_filename"] == "task_73_full_data.jsonl"
    assert row["keyword"] == "fiction"
    assert row["task_name"] == "fixture-task"
    assert row["crawl_time"] == "2026-01-01T00:00:00"
    assert row["publish_time"] is None
    assert row["price"] == 88.0
    assert row["item_id"] == "fixture-item"
    assert row["title"] == "虚构 Q1 限定版"
    assert row["link"] == "https://invalid.example/item?id=fixture"
    assert row["seller_nickname"] == "示例卖家"
    assert row["is_recommended"] == 1
    assert row["analysis_source"] == "keyword"
    assert row["keyword_hit_count"] == 1
    assert row["status"] == "active"
    assert row["search_text"] == normalize_text(build_search_text(record))
    assert json.loads(row["raw_json"])["任务ID"] == 73


def test_pagination_indexes_self_repair_and_query_plans(tmp_path):
    db_path = tmp_path / "app.sqlite3"
    with sqlite_connection(str(db_path)) as conn:
        init_schema(conn)
        conn.execute("DROP INDEX idx_result_items_task_status_price")
        conn.commit()

        init_schema(conn)

        indexes = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert PAGINATION_INDEXES <= indexes
        assert DATA_01A_UNIQUE_INDEXES <= indexes

        traced: list[str] = []
        conn.set_trace_callback(traced.append)
        init_schema(conn)
        conn.set_trace_callback(None)
        normalized_trace = [statement.strip().upper() for statement in traced]
        assert normalized_trace
        assert not any(
            statement.startswith("BEGIN") for statement in normalized_trace
        )
        assert all(
            statement.startswith(("SELECT ", "PRAGMA "))
            for statement in normalized_trace
        )

        expected_sort_indexes = {
            "idx_result_items_task_status_crawl": [
                ("task_id", 0),
                ("status", 0),
                ("crawl_time", 1),
                ("id", 1),
            ],
            "idx_result_items_task_status_publish": [
                ("task_id", 0),
                ("status", 0),
                (None, 1),
                ("id", 1),
            ],
            "idx_result_items_task_status_price": [
                ("task_id", 0),
                ("status", 0),
                (None, 1),
                ("id", 1),
            ],
            "idx_result_items_task_status_keyword_hit_count": [
                ("task_id", 0),
                ("status", 0),
                ("keyword_hit_count", 1),
                ("id", 1),
            ],
            "idx_result_items_legacy_status_crawl": [
                ("result_filename", 0),
                ("status", 0),
                ("crawl_time", 1),
                ("id", 1),
            ],
            "idx_result_items_legacy_status_publish": [
                ("result_filename", 0),
                ("status", 0),
                (None, 1),
                ("id", 1),
            ],
            "idx_result_items_legacy_status_price": [
                ("result_filename", 0),
                ("status", 0),
                (None, 1),
                ("id", 1),
            ],
            "idx_result_items_legacy_status_keyword_hit_count": [
                ("result_filename", 0),
                ("status", 0),
                ("keyword_hit_count", 1),
                ("id", 1),
            ],
        }
        for name, expected_columns in expected_sort_indexes.items():
            assert _index_key_columns(conn, name) == expected_columns
        assert _index_key_columns(
            conn,
            "idx_result_items_task_status_recommended",
        ) == [
            ("task_id", 0),
            ("status", 0),
            ("is_recommended", 0),
            ("analysis_source", 0),
            ("crawl_time", 1),
            ("id", 1),
        ]
        assert _index_key_columns(
            conn,
            "idx_result_items_legacy_status_recommended",
        ) == [
            ("result_filename", 0),
            ("status", 0),
            ("is_recommended", 0),
            ("analysis_source", 0),
            ("crawl_time", 1),
            ("id", 1),
        ]

        task_plan = _production_page_plan(conn, task_id=17)
        task_asc_plan = _production_page_plan(
            conn,
            task_id=17,
            sort_order="asc",
        )
        task_publish_plan = _production_page_plan(
            conn,
            task_id=17,
            sort_by="publish_time",
        )
        task_price_plan = _production_page_plan(
            conn,
            task_id=17,
            sort_by="price",
        )
        legacy_plan = _production_page_plan(
            conn,
            filename="fiction_full_data.jsonl",
        )
        recommended_plan = _production_page_plan(
            conn,
            task_id=17,
            ai_recommended_only=True,
        )
        legacy_recommended_plan = _production_page_plan(
            conn,
            filename="fiction_full_data.jsonl",
            ai_recommended_only=True,
        )

        assert "SEARCH result_items USING INDEX idx_result_items_task_status_crawl" in task_plan
        assert "USE TEMP B-TREE" not in task_plan
        assert "idx_result_items_task_status_crawl" in task_asc_plan
        assert "USE TEMP B-TREE" not in task_asc_plan
        assert "idx_result_items_task_status_publish" in task_publish_plan
        assert "USE TEMP B-TREE" not in task_publish_plan
        assert "idx_result_items_task_status_price" in task_price_plan
        assert "USE TEMP B-TREE" not in task_price_plan
        assert "SEARCH result_items USING INDEX idx_result_items_legacy_status_crawl" in legacy_plan
        assert "USE TEMP B-TREE" not in legacy_plan
        assert "idx_result_items_task_status_recommended" in recommended_plan
        assert "USE TEMP B-TREE" not in recommended_plan
        assert "idx_result_items_legacy_status_recommended" in legacy_recommended_plan
        assert "USE TEMP B-TREE" not in legacy_recommended_plan
