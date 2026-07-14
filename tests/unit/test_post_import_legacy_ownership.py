import json
from contextlib import contextmanager

from src.infrastructure.persistence import sqlite_bootstrap as bootstrap_module
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import (
    TASK_OWNED_DATA_MIGRATION_KEY,
    sqlite_connection,
)


def _result_record(task_name: str, keyword: str, item_id: str) -> dict:
    return {
        "\u4efb\u52a1\u540d\u79f0": task_name,
        "\u641c\u7d22\u5173\u952e\u5b57": keyword,
        "\u722c\u53d6\u65f6\u95f4": "2026-07-14T10:00:00",
        "\u5546\u54c1\u4fe1\u606f": {
            "\u5546\u54c1ID": item_id,
            "\u5f53\u524d\u552e\u4ef7": "100",
        },
    }


def _snapshot_record(task_name: str, keyword: str, item_id: str) -> dict:
    return {
        "task_name": task_name,
        "keyword": keyword,
        "snapshot_time": "2026-07-14T10:00:00",
        "snapshot_day": "2026-07-14",
        "run_id": f"run-{item_id}",
        "item_id": item_id,
        "price": 100,
    }


def _write_jsonl(path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_bootstrap_assigns_post_import_ownership_once(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            [
                {"id": 11, "task_name": "unique-task", "keyword": "camera"},
                {"id": 12, "task_name": "duplicate-task", "keyword": "same"},
                {"id": 13, "task_name": "duplicate-task", "keyword": "same"},
            ]
        ),
        encoding="utf-8",
    )
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    _write_jsonl(
        result_dir / "legacy_full_data.jsonl",
        [
            _result_record("unique-task", "camera", "result-assigned"),
            _result_record("duplicate-task", "same", "result-ambiguous"),
            _result_record("missing-task", "missing", "result-unmatched"),
        ],
    )
    history_dir = tmp_path / "price-history"
    history_dir.mkdir()
    _write_jsonl(
        history_dir / "legacy_history.jsonl",
        [
            _snapshot_record("unique-task", "camera", "snapshot-assigned"),
            _snapshot_record("duplicate-task", "same", "snapshot-ambiguous"),
            _snapshot_record("missing-task", "missing", "snapshot-unmatched"),
        ],
    )
    db_path = tmp_path / "app.sqlite3"
    bootstrap_args = {
        "db_path": str(db_path),
        "legacy_config_file": str(config_path),
        "legacy_result_dir": str(result_dir),
        "legacy_price_history_dir": str(history_dir),
    }

    bootstrap_sqlite_storage(**bootstrap_args)

    with sqlite_connection(str(db_path), read_only=True) as conn:
        result_rows = conn.execute(
            "SELECT item_id, task_id FROM result_items ORDER BY id"
        ).fetchall()
        snapshot_rows = conn.execute(
            "SELECT item_id, task_id FROM price_snapshots ORDER BY id"
        ).fetchall()
        marker_before = conn.execute(
            "SELECT value FROM app_metadata WHERE key = ?",
            (TASK_OWNED_DATA_MIGRATION_KEY,),
        ).fetchone()["value"]

    assert [(row["item_id"], row["task_id"]) for row in result_rows] == [
        ("result-assigned", 11),
        ("result-ambiguous", None),
        ("result-unmatched", None),
    ]
    assert [(row["item_id"], row["task_id"]) for row in snapshot_rows] == [
        ("snapshot-assigned", 11),
        ("snapshot-ambiguous", None),
        ("snapshot-unmatched", None),
    ]
    stats = json.loads(marker_before)
    expected_table_stats = {
        "assigned": 1,
        "unassigned": 1,
        "ambiguous": 1,
        "failed": 0,
    }
    assert stats["result_items"] == expected_table_stats
    assert stats["price_snapshots"] == expected_table_stats
    assert stats["totals"] == {
        "assigned": 2,
        "unassigned": 2,
        "ambiguous": 2,
        "failed": 0,
    }

    statements = []
    original_connection = bootstrap_module.sqlite_connection

    @contextmanager
    def traced_connection(*args, **kwargs):
        with original_connection(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(bootstrap_module, "sqlite_connection", traced_connection)
    bootstrap_sqlite_storage(**bootstrap_args)

    normalized = [statement.strip().upper() for statement in statements]
    assert not any(
        statement.startswith(
            ("BEGIN", "CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ")
        )
        for statement in normalized
    )
    with sqlite_connection(str(db_path), read_only=True) as conn:
        marker_after = conn.execute(
            "SELECT value FROM app_metadata WHERE key = ?",
            (TASK_OWNED_DATA_MIGRATION_KEY,),
        ).fetchone()["value"]
    assert marker_after == marker_before
