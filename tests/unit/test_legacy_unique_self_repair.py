import sqlite3

from src.infrastructure.persistence import sqlite_connection as connection_module
from src.infrastructure.persistence.sqlite_connection import (
    TASK_OWNED_DATA_MIGRATION_KEY,
    TASK_OWNED_INDEXES,
    init_schema,
)


RESULT_SEQUENCE = 70
SNAPSHOT_SEQUENCE = 80
RAW_JSON = '{  "preserve" : [1, 2], "status": "exact"  }'


def _connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _add_unique_constraint(table_sql: str, constraint: str) -> str:
    normalized = table_sql.rstrip()
    assert normalized.endswith(")")
    return f"{normalized[:-1]}, {constraint})"


def _table_sql(conn: sqlite3.Connection, table_name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    assert row is not None
    return str(row["sql"])


def _table_unique_constraints(
    conn: sqlite3.Connection,
    table_name: str,
) -> list[tuple[str, ...]]:
    constraints = []
    for index_row in conn.execute(f"PRAGMA index_list({table_name})").fetchall():
        if index_row["origin"] != "u":
            continue
        columns = tuple(
            str(column_row["name"])
            for column_row in conn.execute(
                f'PRAGMA index_info("{index_row["name"]}")'
            ).fetchall()
        )
        constraints.append(columns)
    return constraints


def _create_deceptive_current_schema(conn: sqlite3.Connection) -> None:
    init_schema(conn)
    result_sql = _table_sql(conn, "result_items")
    snapshot_sql = _table_sql(conn, "price_snapshots")

    conn.execute("DROP TABLE result_items")
    conn.execute("DROP TABLE price_snapshots")
    conn.execute(
        _add_unique_constraint(
            result_sql,
            'CONSTRAINT legacy_result_identity UNIQUE '
            '("result_filename", "link_unique_key") ON CONFLICT ABORT',
        )
    )
    conn.execute(
        _add_unique_constraint(
            snapshot_sql,
            'CONSTRAINT legacy_snapshot_identity UNIQUE '
            '("keyword_slug", "run_id", "item_id") ON CONFLICT ABORT',
        )
    )
    connection_module._ensure_task_owned_indexes(conn)

    conn.execute(
        """
        INSERT INTO result_items (
            id, task_id, result_filename, keyword, task_name, crawl_time,
            item_id, link_unique_key, is_recommended, keyword_hit_count,
            status, raw_json
        ) VALUES (
            7, 101, 'camera_full_data.jsonl', 'camera', 'task-a',
            '2026-01-01T00:00:00', 'item-1', 'same-link', 1, 3,
            'archived', ?
        )
        """,
        (RAW_JSON,),
    )
    conn.execute(
        """
        INSERT INTO price_snapshots (
            id, task_id, keyword_slug, keyword, task_name, snapshot_time,
            snapshot_day, run_id, item_id, price, tags_json
        ) VALUES (
            9, 101, 'camera', 'camera', 'task-a',
            '2026-01-01T00:00:00', '2026-01-01', 'run-1', 'item-1',
            100, '["preserve"]'
        )
        """
    )
    conn.execute(
        "UPDATE sqlite_sequence SET seq = ? WHERE name = 'result_items'",
        (RESULT_SEQUENCE,),
    )
    conn.execute(
        "UPDATE sqlite_sequence SET seq = ? WHERE name = 'price_snapshots'",
        (SNAPSHOT_SEQUENCE,),
    )
    conn.commit()


def test_init_schema_repairs_deceptive_legacy_unique_constraints(tmp_path):
    conn = _connect(tmp_path / "app.sqlite3")
    _create_deceptive_current_schema(conn)

    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_DATA_MIGRATION_KEY,),
    ).fetchone()
    index_names = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert marker is not None
    assert TASK_OWNED_INDEXES <= index_names
    assert "task_id" in {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(result_items)").fetchall()
    }
    assert "task_id" in {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(price_snapshots)").fetchall()
    }
    assert _table_unique_constraints(conn, "result_items") == [
        ("result_filename", "link_unique_key")
    ]
    assert _table_unique_constraints(conn, "price_snapshots") == [
        ("keyword_slug", "run_id", "item_id")
    ]

    repair_statements = []
    conn.set_trace_callback(repair_statements.append)
    init_schema(conn)
    conn.set_trace_callback(None)

    assert "BEGIN IMMEDIATE" in {
        statement.strip().upper() for statement in repair_statements
    }
    result_row = conn.execute(
        "SELECT id, task_id, status, raw_json FROM result_items"
    ).fetchone()
    snapshot_row = conn.execute(
        "SELECT id, task_id, tags_json FROM price_snapshots"
    ).fetchone()
    assert result_row is not None
    assert tuple(result_row) == (7, 101, "archived", RAW_JSON)
    assert snapshot_row is not None
    assert tuple(snapshot_row) == (9, 101, '["preserve"]')
    assert conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 1
    sequences = {
        str(row["name"]): int(row["seq"])
        for row in conn.execute(
            "SELECT name, seq FROM sqlite_sequence "
            "WHERE name IN ('result_items', 'price_snapshots')"
        ).fetchall()
    }
    assert sequences == {
        "result_items": RESULT_SEQUENCE,
        "price_snapshots": SNAPSHOT_SEQUENCE,
    }
    assert _table_unique_constraints(conn, "result_items") == []
    assert _table_unique_constraints(conn, "price_snapshots") == []

    result_cursor = conn.execute(
        """
        INSERT INTO result_items (
            task_id, result_filename, keyword, task_name, crawl_time,
            item_id, link_unique_key, is_recommended, keyword_hit_count,
            status, raw_json
        ) VALUES (
            202, 'camera_full_data.jsonl', 'camera', 'task-b',
            '2026-01-02T00:00:00', 'item-2', 'same-link', 0, 0,
            'active', '{}'
        )
        """
    )
    snapshot_cursor = conn.execute(
        """
        INSERT INTO price_snapshots (
            task_id, keyword_slug, keyword, task_name, snapshot_time,
            snapshot_day, run_id, item_id, price, tags_json
        ) VALUES (
            202, 'camera', 'camera', 'task-b', '2026-01-02T00:00:00',
            '2026-01-02', 'run-1', 'item-1', 101, '[]'
        )
        """
    )
    assert result_cursor.lastrowid == RESULT_SEQUENCE + 1
    assert snapshot_cursor.lastrowid == SNAPSHOT_SEQUENCE + 1
    conn.commit()

    second_init_statements = []
    conn.set_trace_callback(second_init_statements.append)
    init_schema(conn)
    conn.set_trace_callback(None)
    normalized = [
        statement.strip().upper() for statement in second_init_statements
    ]
    assert not any(statement.startswith("BEGIN IMMEDIATE") for statement in normalized)
    assert normalized
    assert all(
        statement.startswith(("SELECT ", "PRAGMA ")) for statement in normalized
    )
    assert not any(
        statement.startswith(
            (
                "CREATE ",
                "ALTER ",
                "DROP ",
                "INSERT ",
                "UPDATE ",
                "DELETE ",
                "REPLACE ",
            )
        )
        for statement in normalized
    )
    assert conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0] == 2
    conn.close()
