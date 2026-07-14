import json
import sqlite3

import pytest

from src.infrastructure.persistence import sqlite_connection as connection_module
from src.infrastructure.persistence.sqlite_connection import (
    SCHEMA_STATEMENTS,
    TASK_OWNED_DATA_MIGRATION_KEY,
    init_schema,
)


OLD_RESULT_ITEMS_SQL = """
CREATE TABLE result_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

OLD_PRICE_SNAPSHOTS_SQL = """
CREATE TABLE price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_slug TEXT NOT NULL,
    keyword TEXT NOT NULL,
    task_name TEXT NOT NULL,
    snapshot_time TEXT NOT NULL,
    snapshot_day TEXT NOT NULL,
    run_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT,
    price REAL NOT NULL,
    price_display TEXT,
    tags_json TEXT NOT NULL,
    region TEXT,
    seller TEXT,
    publish_time TEXT,
    link TEXT,
    UNIQUE(keyword_slug, run_id, item_id)
)
"""


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_old_database(path) -> sqlite3.Connection:
    conn = _connect(path)
    conn.execute(SCHEMA_STATEMENTS[0])
    conn.execute(SCHEMA_STATEMENTS[1])
    conn.execute(OLD_RESULT_ITEMS_SQL)
    conn.execute(OLD_PRICE_SNAPSHOTS_SQL)
    conn.execute(
        """
        INSERT INTO tasks (
            id, task_name, enabled, keyword, description, analyze_images,
            max_pages, personal_only, min_price, max_price, cron,
            ai_prompt_base_file, ai_prompt_criteria_file, account_state_file,
            account_strategy, free_shipping, new_publish_option, region,
            decision_mode, keyword_rules_json, is_running
        ) VALUES (?, ?, 1, ?, '', 1, 1, 1, NULL, NULL, NULL,
                  'prompts/base_prompt.txt', '', NULL, 'auto', 1, NULL, NULL,
                  'keyword', '["camera"]', 0)
        """,
        (1, "unique-task", "camera"),
    )
    for task_id in (2, 3):
        conn.execute(
            """
            INSERT INTO tasks (
                id, task_name, enabled, keyword, description, analyze_images,
                max_pages, personal_only, min_price, max_price, cron,
                ai_prompt_base_file, ai_prompt_criteria_file, account_state_file,
                account_strategy, free_shipping, new_publish_option, region,
                decision_mode, keyword_rules_json, is_running
            ) VALUES (?, 'duplicate-task', 1, 'same', '', 1, 1, 1,
                      NULL, NULL, NULL, 'prompts/base_prompt.txt', '', NULL,
                      'auto', 1, NULL, NULL, 'keyword', '["same"]', 0)
            """,
            (task_id,),
        )
    conn.commit()
    return conn


def _insert_old_result(
    conn: sqlite3.Connection,
    *,
    filename: str,
    task_name: str,
    keyword: str,
    item_id: str,
    raw_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO result_items (
            result_filename, keyword, task_name, crawl_time, item_id,
            link_unique_key, is_recommended, keyword_hit_count, status, raw_json
        ) VALUES (?, ?, ?, '2026-01-01T00:00:00', ?, ?, 0, 0, 'active', ?)
        """,
        (filename, keyword, task_name, item_id, f"item:{item_id}", raw_json),
    )


def _insert_old_snapshot(
    conn: sqlite3.Connection,
    *,
    task_name: str,
    keyword: str,
    run_id: str,
    item_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO price_snapshots (
            keyword_slug, keyword, task_name, snapshot_time, snapshot_day,
            run_id, item_id, price, tags_json
        ) VALUES (?, ?, ?, '2026-01-01T00:00:00', '2026-01-01', ?, ?, 100, '[]')
        """,
        (keyword, keyword, task_name, run_id, item_id),
    )


def test_old_tables_migrate_without_data_loss_and_assign_only_unique_matches(tmp_path):
    conn = _create_old_database(tmp_path / "app.sqlite3")
    raw_json = '{  "preserve" : [1, 2], "text": "fictional"  }'
    _insert_old_result(
        conn,
        filename="camera_full_data.jsonl",
        task_name="unique-task",
        keyword="camera",
        item_id="assigned",
        raw_json=raw_json,
    )
    _insert_old_result(
        conn,
        filename="same_full_data.jsonl",
        task_name="duplicate-task",
        keyword="same",
        item_id="ambiguous",
        raw_json='{"kind":"ambiguous"}',
    )
    _insert_old_result(
        conn,
        filename="camera-other_full_data.jsonl",
        task_name="wrong-name",
        keyword="camera",
        item_id="keyword-only",
        raw_json='{"kind":"keyword-only"}',
    )
    _insert_old_result(
        conn,
        filename="missing_full_data.jsonl",
        task_name="missing-task",
        keyword="missing",
        item_id="missing",
        raw_json='{"kind":"missing"}',
    )
    _insert_old_snapshot(
        conn,
        task_name="unique-task",
        keyword="camera",
        run_id="run-assigned",
        item_id="assigned",
    )
    conn.execute(
        "UPDATE sqlite_sequence SET seq = 50 WHERE name IN ('result_items', 'price_snapshots')"
    )
    conn.commit()
    result_count_before = conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0]
    snapshot_count_before = conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]

    init_schema(conn)

    rows = conn.execute(
        "SELECT item_id, task_id, raw_json, status FROM result_items ORDER BY id"
    ).fetchall()
    snapshots = conn.execute(
        "SELECT item_id, task_id FROM price_snapshots ORDER BY id"
    ).fetchall()
    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_DATA_MIGRATION_KEY,),
    ).fetchone()
    stats = json.loads(marker["value"])

    assert len(rows) == result_count_before == 4
    assert len(snapshots) == snapshot_count_before == 1
    assert rows[0]["task_id"] == 1
    assert rows[0]["raw_json"] == raw_json
    assert rows[0]["status"] == "active"
    assert [row["task_id"] for row in rows[1:]] == [None, None, None]
    assert snapshots[0]["task_id"] == 1
    assert stats["result_items"] == {
        "assigned": 1,
        "unassigned": 2,
        "ambiguous": 1,
        "failed": 0,
    }
    assert stats["price_snapshots"]["assigned"] == 1
    sequences = {
        row["name"]: row["seq"]
        for row in conn.execute(
            "SELECT name, seq FROM sqlite_sequence "
            "WHERE name IN ('result_items', 'price_snapshots')"
        ).fetchall()
    }
    assert sequences == {"result_items": 50, "price_snapshots": 50}

    statements = []
    conn.set_trace_callback(statements.append)
    init_schema(conn)
    normalized = [statement.strip().upper() for statement in statements]
    assert not any(
        statement.startswith(("BEGIN IMMEDIATE", "CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE "))
        for statement in normalized
    )
    conn.close()


def test_task_and_legacy_partial_unique_indexes_are_isolated(tmp_path):
    conn = _create_old_database(tmp_path / "app.sqlite3")
    init_schema(conn)

    base_values = (
        "camera_full_data.jsonl",
        "camera",
        "unique-task",
        "2026-01-01T00:00:00",
        "same-link",
        0,
        0,
        "{}",
    )
    conn.execute(
        """
        INSERT INTO result_items (
            task_id, result_filename, keyword, task_name, crawl_time,
            link_unique_key, is_recommended, keyword_hit_count, raw_json
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        base_values,
    )
    conn.execute(
        """
        INSERT INTO result_items (
            task_id, result_filename, keyword, task_name, crawl_time,
            link_unique_key, is_recommended, keyword_hit_count, raw_json
        ) VALUES (2, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        base_values,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO result_items (
                task_id, result_filename, keyword, task_name, crawl_time,
                link_unique_key, is_recommended, keyword_hit_count, raw_json
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            base_values,
        )

    conn.execute(
        """
        INSERT INTO result_items (
            task_id, result_filename, keyword, task_name, crawl_time,
            link_unique_key, is_recommended, keyword_hit_count, raw_json
        ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        base_values,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO result_items (
                task_id, result_filename, keyword, task_name, crawl_time,
                link_unique_key, is_recommended, keyword_hit_count, raw_json
            ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            base_values,
        )
    other_filename = ("other_full_data.jsonl", *base_values[1:])
    conn.execute(
        """
        INSERT INTO result_items (
            task_id, result_filename, keyword, task_name, crawl_time,
            link_unique_key, is_recommended, keyword_hit_count, raw_json
        ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        other_filename,
    )

    snapshot_values = (
        "camera",
        "camera",
        "unique-task",
        "2026-01-01T00:00:00",
        "2026-01-01",
        "run-1",
        "item-1",
        100,
        "[]",
    )
    for task_id in (1, 2):
        conn.execute(
            """
            INSERT INTO price_snapshots (
                task_id, keyword_slug, keyword, task_name, snapshot_time,
                snapshot_day, run_id, item_id, price, tags_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, *snapshot_values),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO price_snapshots (
                task_id, keyword_slug, keyword, task_name, snapshot_time,
                snapshot_day, run_id, item_id, price, tags_json
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            snapshot_values,
        )
    conn.execute(
        """
        INSERT INTO price_snapshots (
            task_id, keyword_slug, keyword, task_name, snapshot_time,
            snapshot_day, run_id, item_id, price, tags_json
        ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        snapshot_values,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO price_snapshots (
                task_id, keyword_slug, keyword, task_name, snapshot_time,
                snapshot_day, run_id, item_id, price, tags_json
            ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            snapshot_values,
        )
    other_legacy_snapshot = ("other", *snapshot_values[1:])
    conn.execute(
        """
        INSERT INTO price_snapshots (
            task_id, keyword_slug, keyword, task_name, snapshot_time,
            snapshot_day, run_id, item_id, price, tags_json
        ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        other_legacy_snapshot,
    )
    conn.close()


def test_task_owned_table_rebuild_failure_rolls_back_original_tables(tmp_path, monkeypatch):
    conn = _create_old_database(tmp_path / "app.sqlite3")
    raw_json = '{"must":"survive"}'
    _insert_old_result(
        conn,
        filename="camera_full_data.jsonl",
        task_name="unique-task",
        keyword="camera",
        item_id="rollback",
        raw_json=raw_json,
    )
    conn.commit()

    def fail_copy(_conn, _task_id_expression):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(
        connection_module,
        "_copy_result_items_for_task_ownership",
        fail_copy,
    )

    with pytest.raises(RuntimeError, match="copy failed"):
        init_schema(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(result_items)")}
    row = conn.execute("SELECT raw_json FROM result_items").fetchone()
    marker = conn.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (TASK_OWNED_DATA_MIGRATION_KEY,),
    ).fetchone()
    temp_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'result_items__task_owner_migration'"
    ).fetchone()
    assert "task_id" not in columns
    assert row["raw_json"] == raw_json
    assert marker is None
    assert temp_table is None
    conn.close()
