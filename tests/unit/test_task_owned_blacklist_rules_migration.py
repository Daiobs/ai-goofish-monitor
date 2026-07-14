import json
import sqlite3

import pytest

from src.infrastructure.persistence import sqlite_bootstrap as bootstrap_module
from src.infrastructure.persistence import sqlite_connection as connection_module
from src.infrastructure.persistence.sqlite_bootstrap import (
    RESULTS_BOOTSTRAP_KEY,
    SNAPSHOTS_BOOTSTRAP_KEY,
    TASKS_BOOTSTRAP_KEY,
    bootstrap_sqlite_storage,
)
from src.infrastructure.persistence.sqlite_connection import (
    LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,
    TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,
    TASK_OWNED_DATA_MIGRATION_KEY,
    init_schema,
    migrate_task_owned_blacklist_rules,
    sqlite_connection,
)
from src.infrastructure.persistence.storage_names import (
    build_legacy_result_filename,
)
from src.services.result_storage_service import _load_filtered_records_from_conn


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_result(
    conn: sqlite3.Connection,
    *,
    filename: str,
    keyword: str,
    item_id: str,
    task_id: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO result_items (
            task_id, result_filename, keyword, task_name, crawl_time,
            item_id, link_unique_key, is_recommended, keyword_hit_count,
            status, raw_json
        ) VALUES (?, ?, ?, 'fictional task', '2026-07-14T10:00:00',
                  ?, ?, 0, 0, 'active', '{}')
        """,
        (task_id, filename, keyword, item_id, f"item:{item_id}"),
    )


def _insert_task(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    task_name: str,
    keyword: str,
) -> None:
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
                  'keyword', '[]', 0)
        """,
        (task_id, task_name, keyword),
    )


def _insert_filename_rule(
    conn: sqlite3.Connection,
    *,
    filename: str,
    keywords: list[str],
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO result_blacklist_rules (
            result_filename, blacklist_keywords_json, updated_at
        ) VALUES (?, ?, ?)
        """,
        (filename, json.dumps(keywords), updated_at),
    )


def _insert_task_rule(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    keywords: list[str],
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO task_result_blacklist_rules (
            task_id, blacklist_keywords_json, updated_at
        ) VALUES (?, ?, ?)
        """,
        (task_id, json.dumps(keywords), updated_at),
    )


def _rule(conn: sqlite3.Connection, table: str, column: str, value):
    return conn.execute(
        f"SELECT blacklist_keywords_json, updated_at FROM {table} WHERE {column} = ?",
        (value,),
    ).fetchone()


def _marker_payload(conn: sqlite3.Connection, key: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (key,),
    ).fetchone()
    assert row is not None
    return json.loads(row["value"])


def test_migration_merges_distinct_task_targets_and_preserves_shared_legacy_rule(
    tmp_path,
):
    conn = _connect(tmp_path / "app.sqlite3")
    init_schema(conn)
    filename = "fictional_console_full_data.jsonl"
    source_updated_at = "2026-07-14T11:00:00"
    target_updated_at = "2026-07-14T12:00:00"
    for item_id, task_id in (
        ("owned-a-1", 501),
        ("owned-a-2", 501),
        ("owned-b", 502),
        ("unowned", None),
    ):
        _insert_result(
            conn,
            filename=filename,
            keyword="fictional console",
            item_id=item_id,
            task_id=task_id,
        )
    _insert_filename_rule(
        conn,
        filename=filename,
        keywords=[
            "  Arcade Stick  ",
            "arcade stick",
            "alpha,beta",
            r"RE:Limited\s+Run",
            "",
        ],
        updated_at=source_updated_at,
    )
    _insert_task_rule(
        conn,
        task_id=501,
        keywords=["Existing Only", "ARCADE STICK"],
        updated_at=target_updated_at,
    )
    conn.execute(
        "DELETE FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    )
    conn.commit()

    init_schema(conn)

    task_501 = _rule(conn, "task_result_blacklist_rules", "task_id", 501)
    task_502 = _rule(conn, "task_result_blacklist_rules", "task_id", 502)
    assert json.loads(task_501["blacklist_keywords_json"]) == [
        "existing only",
        "arcade stick",
        "alpha",
        "beta",
        r"re:Limited\s+Run",
    ]
    assert task_501["updated_at"] == target_updated_at
    assert json.loads(task_502["blacklist_keywords_json"]) == [
        "arcade stick",
        "alpha",
        "beta",
        r"re:Limited\s+Run",
    ]
    assert task_502["updated_at"] == source_updated_at
    assert _rule(conn, "result_blacklist_rules", "result_filename", filename)

    marker_row = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    ).fetchone()
    assert json.loads(marker_row["value"]) == {
        "failed": 0,
        "legacy_rules_moved": 0,
        "legacy_rules_preserved": 1,
        "task_rules_created": 1,
        "task_rules_merged": 1,
        "task_targets": 2,
    }
    assert filename not in marker_row["value"]
    assert "arcade stick" not in marker_row["value"]

    statements = []
    marker_before = marker_row["value"]
    conn.set_trace_callback(statements.append)
    init_schema(conn)
    conn.set_trace_callback(None)
    assert not any(
        statement.strip().upper().startswith(
            ("BEGIN", "CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ")
        )
        for statement in statements
    )
    marker_after = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    ).fetchone()["value"]
    assert marker_after == marker_before

    statements = []
    conn.set_trace_callback(statements.append)
    forced_stats = migrate_task_owned_blacklist_rules(conn, force=True)
    conn.set_trace_callback(None)
    assert forced_stats == {
        "failed": 0,
        "legacy_rules_moved": 0,
        "legacy_rules_preserved": 1,
        "task_rules_created": 0,
        "task_rules_merged": 0,
        "task_targets": 2,
    }
    assert not any(
        statement.strip().upper().startswith(("INSERT ", "UPDATE ", "DELETE "))
        for statement in statements
    )
    assert conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    ).fetchone()["value"] == marker_before
    conn.close()


def test_migration_removes_fully_owned_source_and_keeps_newest_timestamp(tmp_path):
    conn = _connect(tmp_path / "app.sqlite3")
    init_schema(conn)
    filename = "fictional_lens_full_data.jsonl"
    _insert_result(
        conn,
        filename=filename,
        keyword="fictional lens",
        item_id="owned",
        task_id=603,
    )
    _insert_filename_rule(
        conn,
        filename=filename,
        keywords=["Source Only", "TARGET KEEP"],
        updated_at="2026-07-14T12:00:00",
    )
    _insert_task_rule(
        conn,
        task_id=603,
        keywords=["Target Keep"],
        updated_at="2026-07-14T09:00:00",
    )
    conn.execute(
        "DELETE FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    )
    conn.commit()

    init_schema(conn)

    target = _rule(conn, "task_result_blacklist_rules", "task_id", 603)
    assert json.loads(target["blacklist_keywords_json"]) == [
        "target keep",
        "source only",
    ]
    assert target["updated_at"] == "2026-07-14T12:00:00"
    assert _rule(
        conn,
        "result_blacklist_rules",
        "result_filename",
        filename,
    ) is None
    assert _marker_payload(conn, TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY) == {
        "failed": 0,
        "legacy_rules_moved": 1,
        "legacy_rules_preserved": 0,
        "task_rules_created": 0,
        "task_rules_merged": 1,
        "task_targets": 1,
    }
    conn.close()


def test_namespace_copies_rules_and_only_deletes_sources_without_null_rows(tmp_path):
    conn = _connect(tmp_path / "app.sqlite3")
    init_schema(conn)
    fully_moved_source = "task_701_full_data.jsonl"
    partially_moved_source = "task_702_full_data.jsonl"
    fully_moved_target = build_legacy_result_filename("task_701")
    partially_moved_target = build_legacy_result_filename("task_702")

    for source, task_id in (
        (fully_moved_source, 701),
        (partially_moved_source, 702),
    ):
        keyword = f"task_{task_id}"
        _insert_result(
            conn,
            filename=source,
            keyword=keyword,
            item_id=f"owned-{task_id}",
            task_id=task_id,
        )
        _insert_result(
            conn,
            filename=source,
            keyword=keyword,
            item_id=f"unowned-{task_id}",
            task_id=None,
        )
        _insert_filename_rule(
            conn,
            filename=source,
            keywords=[f"Source {task_id}", "SHARED"],
            updated_at="2026-07-14T11:00:00",
        )
    _insert_result(
        conn,
        filename=partially_moved_source,
        keyword="fictional-other",
        item_id="unowned-stays",
        task_id=None,
    )
    _insert_filename_rule(
        conn,
        filename=fully_moved_target,
        keywords=["Escaped Existing", "shared"],
        updated_at="2026-07-14T12:00:00",
    )
    conn.execute(
        "DELETE FROM app_metadata WHERE key IN (?, ?)",
        (
            TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,
            LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,
        ),
    )
    conn.commit()

    init_schema(conn)

    assert _rule(
        conn,
        "result_blacklist_rules",
        "result_filename",
        fully_moved_source,
    ) is None
    assert _rule(
        conn,
        "result_blacklist_rules",
        "result_filename",
        partially_moved_source,
    ) is not None
    escaped = _rule(
        conn,
        "result_blacklist_rules",
        "result_filename",
        fully_moved_target,
    )
    assert json.loads(escaped["blacklist_keywords_json"]) == [
        "escaped existing",
        "shared",
        "source 701",
    ]
    assert escaped["updated_at"] == "2026-07-14T12:00:00"
    partial_escaped = _rule(
        conn,
        "result_blacklist_rules",
        "result_filename",
        partially_moved_target,
    )
    assert json.loads(partial_escaped["blacklist_keywords_json"]) == [
        "source 702",
        "shared",
    ]
    remaining = conn.execute(
        """
        SELECT result_filename, keyword
        FROM result_items
        WHERE task_id IS NULL
        ORDER BY keyword
        """
    ).fetchall()
    assert [(row["result_filename"], row["keyword"]) for row in remaining] == [
        (partially_moved_source, "fictional-other"),
        (fully_moved_target, "task_701"),
        (partially_moved_target, "task_702"),
    ]
    assert _marker_payload(conn, TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY) == {
        "failed": 0,
        "legacy_rules_moved": 1,
        "legacy_rules_preserved": 1,
        "task_rules_created": 2,
        "task_rules_merged": 0,
        "task_targets": 2,
    }
    namespace_stats = _marker_payload(
        conn,
        LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,
    )
    assert namespace_stats == {"renamed_rows": 2, "renamed_rule_keys": 1}
    conn.close()


def test_task_rule_migration_failure_rolls_back_partial_merges_and_marker(
    tmp_path,
    monkeypatch,
):
    conn = _connect(tmp_path / "app.sqlite3")
    init_schema(conn)
    filename = "fictional_rollback_full_data.jsonl"
    for task_id in (901, 902):
        _insert_result(
            conn,
            filename=filename,
            keyword="fictional rollback",
            item_id=f"owned-{task_id}",
            task_id=task_id,
        )
    _insert_filename_rule(
        conn,
        filename=filename,
        keywords=["Must Survive"],
        updated_at="2026-07-14T10:00:00",
    )
    conn.execute(
        "DELETE FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    )
    conn.commit()

    original_merge = connection_module._merge_task_blacklist_rule
    calls = 0

    def fail_second_merge(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fictional task rule migration failure")
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(
        connection_module,
        "_merge_task_blacklist_rule",
        fail_second_merge,
    )

    with pytest.raises(RuntimeError, match="fictional task rule migration failure"):
        init_schema(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM task_result_blacklist_rules"
    ).fetchone()[0] == 0
    source = _rule(conn, "result_blacklist_rules", "result_filename", filename)
    assert json.loads(source["blacklist_keywords_json"]) == ["Must Survive"]
    assert conn.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    ).fetchone() is None
    conn.close()


def test_failure_after_namespace_rolls_back_ownership_rules_names_and_markers(
    tmp_path,
    monkeypatch,
):
    conn = _connect(tmp_path / "app.sqlite3")
    init_schema(conn)
    source = "task_42_full_data.jsonl"
    escaped = build_legacy_result_filename("task_42")
    _insert_task(conn, task_id=42, task_name="owned task", keyword="task_42")
    _insert_task(conn, task_id=43, task_name="fictional task", keyword="camera")
    _insert_result(
        conn,
        filename=source,
        keyword="task_42",
        item_id="owned",
        task_id=42,
    )
    _insert_result(
        conn,
        filename=source,
        keyword="task_42",
        item_id="legacy",
        task_id=None,
    )
    _insert_result(
        conn,
        filename="camera_full_data.jsonl",
        keyword="camera",
        item_id="assign-on-retry",
        task_id=None,
    )
    _insert_filename_rule(
        conn,
        filename=source,
        keywords=["Rollback Rule"],
        updated_at="2026-07-14T10:00:00",
    )
    conn.execute(
        "DELETE FROM app_metadata WHERE key IN (?, ?, ?)",
        (
            TASK_OWNED_DATA_MIGRATION_KEY,
            TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,
            LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,
        ),
    )
    conn.commit()

    real_record_moves = connection_module._record_namespace_blacklist_moves

    def fail_after_namespace_moves(connection, moved_count):
        real_record_moves(connection, moved_count)
        raise RuntimeError("fictional namespace completion failure")

    monkeypatch.setattr(
        connection_module,
        "_record_namespace_blacklist_moves",
        fail_after_namespace_moves,
    )

    with pytest.raises(RuntimeError, match="fictional namespace completion failure"):
        init_schema(conn)

    rows = conn.execute(
        "SELECT item_id, task_id, result_filename FROM result_items ORDER BY item_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("assign-on-retry", None, "camera_full_data.jsonl"),
        ("legacy", None, source),
        ("owned", 42, source),
    ]
    assert conn.execute(
        "SELECT COUNT(*) FROM task_result_blacklist_rules"
    ).fetchone()[0] == 0
    assert _rule(conn, "result_blacklist_rules", "result_filename", source)
    assert _rule(conn, "result_blacklist_rules", "result_filename", escaped) is None
    for key in (
        TASK_OWNED_DATA_MIGRATION_KEY,
        TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,
        LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,
    ):
        assert conn.execute(
            "SELECT 1 FROM app_metadata WHERE key = ?",
            (key,),
        ).fetchone() is None
    conn.close()


def _write_bootstrap_fixture(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "id": 811,
                    "task_name": "fictional imported task",
                    "keyword": "fictional_camera",
                }
            ]
        ),
        encoding="utf-8",
    )
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    filename = "fictional_camera_full_data.jsonl"
    record = {
        "任务名称": "fictional imported task",
        "搜索关键字": "fictional_camera",
        "爬取时间": "2026-07-14T10:00:00",
        "商品信息": {
            "商品ID": "fictional-item",
            "商品标题": "Imported Rule console",
            "当前售价": "100",
        },
    }
    (result_dir / filename).write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return filename, config_path, result_dir


def _prepare_bootstrap_database(db_path, filename: str) -> str:
    conn = _connect(db_path)
    init_schema(conn)
    _insert_filename_rule(
        conn,
        filename=filename,
        keywords=["Imported Rule"],
        updated_at="2026-07-14T10:00:00",
    )
    marker_before = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    ).fetchone()["value"]
    conn.commit()
    conn.close()
    return marker_before


def test_jsonl_import_assigns_ownership_then_migrates_rules_in_one_transaction(
    tmp_path,
):
    filename, config_path, result_dir = _write_bootstrap_fixture(tmp_path)
    db_path = tmp_path / "app.sqlite3"
    _prepare_bootstrap_database(db_path, filename)

    bootstrap_sqlite_storage(
        str(db_path),
        legacy_config_file=str(config_path),
        legacy_result_dir=str(result_dir),
        legacy_price_history_dir=str(tmp_path / "price-history"),
    )

    with sqlite_connection(str(db_path)) as conn:
        imported = conn.execute(
            "SELECT task_id, result_filename FROM result_items"
        ).fetchone()
        assert tuple(imported) == (811, filename)
        target = _rule(conn, "task_result_blacklist_rules", "task_id", 811)
        assert json.loads(target["blacklist_keywords_json"]) == ["imported rule"]
        visible = _load_filtered_records_from_conn(
            conn,
            filename=None,
            task_id=811,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            sort_by="crawl_time",
            sort_order="desc",
            include_hidden=False,
        )
        assert visible == []
        assert _rule(
            conn,
            "result_blacklist_rules",
            "result_filename",
            filename,
        ) is None
        assert _marker_payload(conn, TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY) == {
            "failed": 0,
            "legacy_rules_moved": 1,
            "legacy_rules_preserved": 0,
            "task_rules_created": 1,
            "task_rules_merged": 0,
            "task_targets": 1,
        }


def test_jsonl_import_failure_rolls_back_import_ownership_rules_and_markers(
    tmp_path,
    monkeypatch,
):
    filename, config_path, result_dir = _write_bootstrap_fixture(tmp_path)
    db_path = tmp_path / "app.sqlite3"
    marker_before = _prepare_bootstrap_database(db_path, filename)
    real_migration = bootstrap_module.migrate_task_owned_blacklist_rules

    def fail_after_rule_migration(conn, *, force=False):
        real_migration(conn, force=force)
        raise RuntimeError("fictional post-import migration failure")

    monkeypatch.setattr(
        bootstrap_module,
        "migrate_task_owned_blacklist_rules",
        fail_after_rule_migration,
    )

    with pytest.raises(RuntimeError, match="fictional post-import migration failure"):
        bootstrap_sqlite_storage(
            str(db_path),
            legacy_config_file=str(config_path),
            legacy_result_dir=str(result_dir),
            legacy_price_history_dir=str(tmp_path / "price-history"),
        )

    conn = _connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM task_result_blacklist_rules"
    ).fetchone()[0] == 0
    assert _rule(
        conn,
        "result_blacklist_rules",
        "result_filename",
        filename,
    ) is not None
    marker_after = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    ).fetchone()["value"]
    assert marker_after == marker_before
    for key in (TASKS_BOOTSTRAP_KEY, RESULTS_BOOTSTRAP_KEY, SNAPSHOTS_BOOTSTRAP_KEY):
        assert conn.execute(
            "SELECT 1 FROM app_metadata WHERE key = ?",
            (key,),
        ).fetchone() is None
    conn.close()
