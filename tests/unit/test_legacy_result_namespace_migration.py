import json
import sqlite3

import pytest

from src.infrastructure.persistence import sqlite_connection as connection_module
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import (
    LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,
    init_schema,
)
from src.infrastructure.persistence.storage_names import (
    build_legacy_result_filename,
)


SOURCE_FILENAME = "task_42_full_data.jsonl"
KEYWORD = "task_42"
RAW_JSON = '{  "preserve" : [1, 2], "owner": "legacy"  }'


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_pre_namespace_schema(conn: sqlite3.Connection) -> None:
    init_schema(conn)
    conn.execute(
        "DELETE FROM app_metadata WHERE key = ?",
        (LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,),
    )
    conn.execute(
        """
        INSERT INTO result_items (
            task_id, result_filename, keyword, task_name, crawl_time,
            item_id, link_unique_key, is_recommended, keyword_hit_count,
            status, raw_json
        ) VALUES (
            NULL, ?, ?, 'legacy config task', '2026-07-14T10:00:00',
            'item-1', 'item:item-1', 1, 1, 'hidden', ?
        )
        """,
        (SOURCE_FILENAME, KEYWORD, RAW_JSON),
    )
    conn.execute(
        """
        INSERT INTO result_blacklist_rules (
            result_filename, blacklist_keywords_json, updated_at
        ) VALUES (?, '["legacy-only"]', '2026-07-14T10:00:00')
        """,
        (SOURCE_FILENAME,),
    )
    conn.commit()


def test_init_schema_migrates_colliding_legacy_rows_and_blacklist_atomically(tmp_path):
    conn = _connect(tmp_path / "app.sqlite3")
    _seed_pre_namespace_schema(conn)
    target = build_legacy_result_filename(KEYWORD)

    init_schema(conn)

    row = conn.execute(
        """
        SELECT task_id, result_filename, keyword, status, raw_json
        FROM result_items
        """
    ).fetchone()
    assert row is not None
    assert tuple(row) == (None, target, KEYWORD, "hidden", RAW_JSON)
    assert conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0] == 1
    assert conn.execute(
        "SELECT 1 FROM result_blacklist_rules WHERE result_filename = ?",
        (SOURCE_FILENAME,),
    ).fetchone() is None
    rule = conn.execute(
        """
        SELECT blacklist_keywords_json
        FROM result_blacklist_rules
        WHERE result_filename = ?
        """,
        (target,),
    ).fetchone()
    assert json.loads(rule["blacklist_keywords_json"]) == ["legacy-only"]

    trace = []
    conn.set_trace_callback(trace.append)
    init_schema(conn)
    conn.set_trace_callback(None)
    normalized = [statement.strip().upper() for statement in trace]
    assert not any(statement.startswith("BEGIN IMMEDIATE") for statement in normalized)
    assert not any(
        statement.startswith(
            ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ")
        )
        for statement in normalized
    )
    conn.close()


def test_namespace_migration_failure_rolls_back_rows_rules_and_marker(
    tmp_path,
    monkeypatch,
):
    conn = _connect(tmp_path / "app.sqlite3")
    _seed_pre_namespace_schema(conn)

    def fail_rule_migration(_conn, _targets):
        raise RuntimeError("fictional migration failure")

    monkeypatch.setattr(
        connection_module,
        "_migrate_legacy_blacklist_rule_keys",
        fail_rule_migration,
    )

    with pytest.raises(RuntimeError, match="fictional migration failure"):
        init_schema(conn)

    row = conn.execute(
        "SELECT result_filename, raw_json FROM result_items"
    ).fetchone()
    assert tuple(row) == (SOURCE_FILENAME, RAW_JSON)
    assert conn.execute(
        "SELECT 1 FROM result_blacklist_rules WHERE result_filename = ?",
        (SOURCE_FILENAME,),
    ).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,),
    ).fetchone() is None
    conn.close()


def test_post_import_collision_is_escaped_in_the_import_transaction(tmp_path):
    result_dir = tmp_path / "jsonl"
    result_dir.mkdir()
    record = {
        "爬取时间": "2026-07-14T10:00:00",
        "搜索关键字": KEYWORD,
        "任务名称": "legacy config task",
        "商品信息": {
            "商品ID": "item-1",
            "商品链接": "https://www.goofish.com/item?id=item-1",
            "当前售价": "100",
        },
        "ai_analysis": {"is_recommended": True},
    }
    (result_dir / SOURCE_FILENAME).write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "app.sqlite3"

    bootstrap_sqlite_storage(
        str(db_path),
        legacy_config_file=None,
        legacy_result_dir=str(result_dir),
        legacy_price_history_dir=str(tmp_path / "price_history"),
    )

    conn = _connect(db_path)
    row = conn.execute(
        "SELECT task_id, result_filename, keyword FROM result_items"
    ).fetchone()
    assert tuple(row) == (
        None,
        build_legacy_result_filename(KEYWORD),
        KEYWORD,
    )
    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,),
    ).fetchone()
    assert marker is not None
    assert json.loads(marker["value"])["renamed_rows"] == 1
    conn.close()
