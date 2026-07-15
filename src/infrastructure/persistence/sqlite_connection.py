"""
SQLite 连接与 schema 初始化。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from src.keyword_rule_engine import build_search_text, normalize_text
from src.infrastructure.persistence.storage_names import (
    DEFAULT_DATABASE_PATH,
    build_legacy_result_filename,
    build_result_filename,
)
from src.services.result_blacklist_service import (
    is_valid_result_record_structure,
    match_blacklist_search_text,
    normalize_blacklist_keywords,
)


BUSY_TIMEOUT_MS = 5000

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_name TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        keyword TEXT NOT NULL,
        description TEXT,
        analyze_images INTEGER NOT NULL,
        max_pages INTEGER NOT NULL,
        personal_only INTEGER NOT NULL,
        min_price TEXT,
        max_price TEXT,
        cron TEXT,
        ai_prompt_base_file TEXT NOT NULL,
        ai_prompt_criteria_file TEXT NOT NULL,
        account_state_file TEXT,
        account_strategy TEXT NOT NULL,
        free_shipping INTEGER NOT NULL,
        new_publish_option TEXT,
        region TEXT,
        decision_mode TEXT NOT NULL,
        keyword_rules_json TEXT NOT NULL,
        is_running INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS result_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
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
        search_text TEXT NOT NULL DEFAULT '',
        raw_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
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
        link TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS result_blacklist_rules (
        result_filename TEXT PRIMARY KEY,
        blacklist_keywords_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_result_blacklist_rules (
        task_id INTEGER PRIMARY KEY,
        blacklist_keywords_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
        session_id TEXT PRIMARY KEY,
        credential_fingerprint TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_name ON tasks(task_name)",
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_crawl
    ON result_items(result_filename, crawl_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_publish
    ON result_items(result_filename, publish_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_price
    ON result_items(result_filename, price DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_recommended
    ON result_items(result_filename, is_recommended, analysis_source, crawl_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_time
    ON price_snapshots(keyword_slug, snapshot_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_item_time
    ON price_snapshots(keyword_slug, item_id, snapshot_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires
    ON auth_sessions(expires_at)
    """,
)

TASK_IDENTITY_MIGRATION_KEY = "migration:tasks_autoincrement_v1"
RESULT_STATUS_MIGRATION_KEY = "migration:result_items_status"
RESULT_SEARCH_TEXT_MIGRATION_KEY = "migration:result_search_text_v1"
RESULT_STATUS_INDEX_NAME = "idx_results_filename_status_crawl"
TASK_OWNED_DATA_MIGRATION_KEY = "migration:task_owned_results_v1"
TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY = (
    "migration:task_owned_blacklist_rules_v1"
)
LEGACY_RESULT_NAMESPACE_MIGRATION_KEY = (
    "migration:legacy_result_filename_namespace_v1"
)
LEGACY_RESULT_ITEMS_UNIQUE = ("result_filename", "link_unique_key")
LEGACY_PRICE_SNAPSHOTS_UNIQUE = ("keyword_slug", "run_id", "item_id")
PAGINATION_INDEX_STATEMENTS = (
    (
        "idx_result_items_task_status_crawl",
        """
        CREATE INDEX idx_result_items_task_status_crawl
        ON result_items(
            task_id, status, crawl_time DESC, id DESC
        ) WHERE task_id IS NOT NULL
        """,
    ),
    (
        "idx_result_items_task_status_publish",
        """
        CREATE INDEX idx_result_items_task_status_publish
        ON result_items(
            task_id, status, COALESCE(publish_time, '') DESC, id DESC
        ) WHERE task_id IS NOT NULL
        """,
    ),
    (
        "idx_result_items_task_status_price",
        """
        CREATE INDEX idx_result_items_task_status_price
        ON result_items(
            task_id, status, COALESCE(price, 0) DESC, id DESC
        ) WHERE task_id IS NOT NULL
        """,
    ),
    (
        "idx_result_items_task_status_keyword_hit_count",
        """
        CREATE INDEX idx_result_items_task_status_keyword_hit_count
        ON result_items(
            task_id, status, keyword_hit_count DESC, id DESC
        ) WHERE task_id IS NOT NULL
        """,
    ),
    (
        "idx_result_items_task_status_recommended",
        """
        CREATE INDEX idx_result_items_task_status_recommended
        ON result_items(
            task_id, status, is_recommended, analysis_source,
            crawl_time DESC, id DESC
        ) WHERE task_id IS NOT NULL
        """,
    ),
    (
        "idx_result_items_legacy_status_crawl",
        """
        CREATE INDEX idx_result_items_legacy_status_crawl
        ON result_items(
            result_filename, status, crawl_time DESC, id DESC
        ) WHERE task_id IS NULL
        """,
    ),
    (
        "idx_result_items_legacy_status_publish",
        """
        CREATE INDEX idx_result_items_legacy_status_publish
        ON result_items(
            result_filename, status, COALESCE(publish_time, '') DESC, id DESC
        ) WHERE task_id IS NULL
        """,
    ),
    (
        "idx_result_items_legacy_status_price",
        """
        CREATE INDEX idx_result_items_legacy_status_price
        ON result_items(
            result_filename, status, COALESCE(price, 0) DESC, id DESC
        ) WHERE task_id IS NULL
        """,
    ),
    (
        "idx_result_items_legacy_status_keyword_hit_count",
        """
        CREATE INDEX idx_result_items_legacy_status_keyword_hit_count
        ON result_items(
            result_filename, status, keyword_hit_count DESC, id DESC
        ) WHERE task_id IS NULL
        """,
    ),
    (
        "idx_result_items_legacy_status_recommended",
        """
        CREATE INDEX idx_result_items_legacy_status_recommended
        ON result_items(
            result_filename, status, is_recommended, analysis_source,
            crawl_time DESC, id DESC
        ) WHERE task_id IS NULL
        """,
    ),
)
PAGINATION_INDEXES = {name for name, _ in PAGINATION_INDEX_STATEMENTS}
TASK_OWNED_INDEXES = {
    "idx_result_items_task_link_unique",
    "idx_result_items_legacy_file_link_unique",
    "idx_result_items_task_crawl",
    "idx_price_snapshots_task_run_item_unique",
    "idx_price_snapshots_legacy_run_item_unique",
    "idx_price_snapshots_task_time",
    *PAGINATION_INDEXES,
}
REQUIRED_TABLES = {
    "app_metadata",
    "tasks",
    "result_items",
    "price_snapshots",
    "result_blacklist_rules",
    "task_result_blacklist_rules",
    "auth_sessions",
}
REQUIRED_INDEXES = {
    "idx_tasks_name",
    "idx_results_filename_crawl",
    "idx_results_filename_publish",
    "idx_results_filename_price",
    "idx_results_filename_recommended",
    RESULT_STATUS_INDEX_NAME,
    "idx_snapshots_keyword_time",
    "idx_snapshots_keyword_item_time",
    "idx_auth_sessions_expires",
    *TASK_OWNED_INDEXES,
}
TASK_COLUMNS = (
    "id",
    "task_name",
    "enabled",
    "keyword",
    "description",
    "analyze_images",
    "max_pages",
    "personal_only",
    "min_price",
    "max_price",
    "cron",
    "ai_prompt_base_file",
    "ai_prompt_criteria_file",
    "account_state_file",
    "account_strategy",
    "free_shipping",
    "new_publish_option",
    "region",
    "decision_mode",
    "keyword_rules_json",
    "is_running",
)


def get_database_path() -> str:
    return os.getenv("APP_DATABASE_FILE", DEFAULT_DATABASE_PATH)


def _prepare_database_file(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=128)
def _parse_blacklist_rules_json(rules_json: str) -> tuple[str, ...] | None:
    try:
        decoded = json.loads(rules_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if decoded is None:
        return ()
    if not isinstance(decoded, (list, str)):
        return None
    return tuple(normalize_blacklist_keywords(decoded))


def _result_blacklist_match(search_text, rules_json) -> int:
    """SQLite adapter: malformed inputs and unexpected failures hide the row."""
    try:
        if rules_json is None:
            return 0
        rules_text = str(rules_json).strip()
        if rules_text in {"", "[]", "null"}:
            return 0
        keywords = _parse_blacklist_rules_json(rules_text)
        if keywords is None:
            return 1
        if not keywords:
            return 0
        matched = match_blacklist_search_text(str(search_text or ""), keywords)
        return 1 if matched else 0
    except Exception:
        return 1


def _register_sqlite_functions(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "result_blacklist_match",
        2,
        _result_blacklist_match,
        deterministic=True,
    )


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def _apply_read_only_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA query_only=ON")


def init_schema(conn: sqlite3.Connection) -> None:
    if _schema_is_current(conn):
        return

    try:
        conn.execute("BEGIN IMMEDIATE")
        if _schema_is_current(conn):
            conn.commit()
            return
        if not _base_schema_objects_exist(conn):
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
        _migrate_tasks_autoincrement(conn)
        _migrate_result_items_status(conn)
        _migrate_task_owned_data(conn)
        _migrate_result_search_text(conn)
        migrate_task_owned_blacklist_rules(conn)
        migrate_legacy_result_filename_namespace(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """Check schema readiness without starting a write transaction."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index')"
    ).fetchall()
    tables = {str(row["name"]): row for row in rows if row["type"] == "table"}
    indexes = {str(row["name"]) for row in rows if row["type"] == "index"}
    if not REQUIRED_TABLES.issubset(tables) or not REQUIRED_INDEXES.issubset(indexes):
        return False

    tasks_sql = str(tables["tasks"]["sql"] or "")
    if "AUTOINCREMENT" not in tasks_sql.upper():
        return False

    result_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(result_items)").fetchall()
    }
    snapshot_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(price_snapshots)").fetchall()
    }
    if not {"status", "task_id", "search_text"}.issubset(result_columns):
        return False
    if "task_id" not in snapshot_columns:
        return False
    if _has_table_unique_constraint(
        conn,
        "result_items",
        LEGACY_RESULT_ITEMS_UNIQUE,
    ):
        return False
    if _has_table_unique_constraint(
        conn,
        "price_snapshots",
        LEGACY_PRICE_SNAPSHOTS_UNIQUE,
    ):
        return False

    migration_rows = conn.execute(
        "SELECT key FROM app_metadata WHERE key IN (?, ?, ?, ?, ?, ?)",
        (
            TASK_IDENTITY_MIGRATION_KEY,
            RESULT_STATUS_MIGRATION_KEY,
            RESULT_SEARCH_TEXT_MIGRATION_KEY,
            TASK_OWNED_DATA_MIGRATION_KEY,
            TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,
            LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,
        ),
    ).fetchall()
    completed = {str(row["key"]) for row in migration_rows}
    return completed == {
        TASK_IDENTITY_MIGRATION_KEY,
        RESULT_STATUS_MIGRATION_KEY,
        RESULT_SEARCH_TEXT_MIGRATION_KEY,
        TASK_OWNED_DATA_MIGRATION_KEY,
        TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,
        LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,
    }


def _base_schema_objects_exist(conn: sqlite3.Connection) -> bool:
    """Check objects created by SCHEMA_STATEMENTS, excluding status migration."""
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index')"
    ).fetchall()
    tables = {str(row["name"]) for row in rows if row["type"] == "table"}
    indexes = {str(row["name"]) for row in rows if row["type"] == "index"}
    base_indexes = REQUIRED_INDEXES - {RESULT_STATUS_INDEX_NAME} - TASK_OWNED_INDEXES
    return REQUIRED_TABLES.issubset(tables) and base_indexes.issubset(indexes)


def _migrate_tasks_autoincrement(conn: sqlite3.Connection) -> None:
    """Upgrade tasks.id without changing any existing task identity."""
    migration_done = conn.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (TASK_IDENTITY_MIGRATION_KEY,),
    ).fetchone()
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
    ).fetchone()
    table_sql = str(table_row["sql"] if table_row else "")
    has_autoincrement = "AUTOINCREMENT" in table_sql.upper()
    if has_autoincrement and migration_done is not None:
        return

    if not has_autoincrement:
        conn.execute("DROP TABLE IF EXISTS tasks__task_id_migration")
        conn.execute(
            """
            CREATE TABLE tasks__task_id_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                description TEXT,
                analyze_images INTEGER NOT NULL,
                max_pages INTEGER NOT NULL,
                personal_only INTEGER NOT NULL,
                min_price TEXT,
                max_price TEXT,
                cron TEXT,
                ai_prompt_base_file TEXT NOT NULL,
                ai_prompt_criteria_file TEXT NOT NULL,
                account_state_file TEXT,
                account_strategy TEXT NOT NULL,
                free_shipping INTEGER NOT NULL,
                new_publish_option TEXT,
                region TEXT,
                decision_mode TEXT NOT NULL,
                keyword_rules_json TEXT NOT NULL,
                is_running INTEGER NOT NULL
            )
            """
        )
        _copy_tasks_to_autoincrement_table(conn)
        conn.execute("DROP TABLE tasks")
        conn.execute("ALTER TABLE tasks__task_id_migration RENAME TO tasks")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_name ON tasks(task_name)")

    sync_tasks_autoincrement_sequence(conn)
    conn.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, 'done')",
        (TASK_IDENTITY_MIGRATION_KEY,),
    )


def _copy_tasks_to_autoincrement_table(conn: sqlite3.Connection) -> None:
    columns = ", ".join(TASK_COLUMNS)
    conn.execute(
        f"INSERT INTO tasks__task_id_migration ({columns}) SELECT {columns} FROM tasks"
    )


def sync_tasks_autoincrement_sequence(conn: sqlite3.Connection) -> None:
    """Advance SQLite's durable task sequence past every preserved explicit ID."""
    sequence_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'tasks'"
    ).fetchone()
    max_id_row = conn.execute("SELECT MAX(id) AS max_id FROM tasks").fetchone()
    current_sequence = (
        int(sequence_row["seq"]) if sequence_row is not None else None
    )
    max_id = (
        int(max_id_row["max_id"])
        if max_id_row is not None and max_id_row["max_id"] is not None
        else None
    )

    if current_sequence is None:
        if max_id is None:
            return
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES ('tasks', ?)",
            (max_id,),
        )
        return

    if max_id is not None and max_id > current_sequence:
        conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'tasks'",
            (max_id,),
        )


def _migrate_result_items_status(conn: sqlite3.Connection) -> None:
    """Repair the result status column, index, and completion marker."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(result_items)").fetchall()]
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE result_items ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    index_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (RESULT_STATUS_INDEX_NAME,),
    ).fetchone()
    if index_exists is None:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {RESULT_STATUS_INDEX_NAME}"
            " ON result_items(result_filename, status, crawl_time DESC)"
        )
    _write_result_status_migration_marker(conn)


def _write_result_status_migration_marker(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, 'done')",
        (RESULT_STATUS_MIGRATION_KEY,),
    )


def _migrate_result_search_text(conn: sqlite3.Connection) -> None:
    """Add and backfill normalized blacklist-search text without touching raw JSON."""
    columns = _table_columns(conn, "result_items")
    marker = conn.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (RESULT_SEARCH_TEXT_MIGRATION_KEY,),
    ).fetchone()
    if "search_text" in columns and marker is not None:
        return

    if "search_text" not in columns:
        conn.execute(
            "ALTER TABLE result_items "
            "ADD COLUMN search_text TEXT NOT NULL DEFAULT ''"
        )

    stats = {"rows": 0, "invalid_json": 0}
    cursor = conn.execute("SELECT id, raw_json FROM result_items ORDER BY id")
    while True:
        rows = cursor.fetchmany(500)
        if not rows:
            break
        updates: list[tuple[str, int]] = []
        for row in rows:
            stats["rows"] += 1
            try:
                record = json.loads(row["raw_json"])
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                stats["invalid_json"] += 1
                search_text = ""
            else:
                if not is_valid_result_record_structure(record):
                    stats["invalid_json"] += 1
                    search_text = ""
                else:
                    try:
                        search_text = normalize_text(build_search_text(record))
                    except AttributeError:
                        stats["invalid_json"] += 1
                        search_text = ""
            updates.append((search_text, int(row["id"])))
        conn.executemany(
            "UPDATE result_items SET search_text = ? WHERE id = ?",
            updates,
        )

    conn.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
        (
            RESULT_SEARCH_TEXT_MIGRATION_KEY,
            json.dumps(stats, sort_keys=True, separators=(",", ":")),
        ),
    )


def _migrate_task_owned_data(conn: sqlite3.Connection) -> None:
    """Bind online result data to task IDs while preserving legacy NULL rows."""
    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_DATA_MIGRATION_KEY,),
    ).fetchone()
    result_rebuilt = _rebuild_result_items_for_task_ownership(conn)
    snapshots_rebuilt = _rebuild_price_snapshots_for_task_ownership(conn)
    _ensure_task_owned_indexes(conn)

    if marker is not None and not result_rebuilt and not snapshots_rebuilt:
        return

    assign_legacy_task_ownership(conn)


def assign_legacy_task_ownership(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, int]]:
    """Assign unowned rows and refresh ownership migration statistics."""
    result_stats = _assign_legacy_rows_to_tasks(conn, "result_items")
    snapshot_stats = _assign_legacy_rows_to_tasks(conn, "price_snapshots")
    totals = {
        key: result_stats[key] + snapshot_stats[key]
        for key in ("assigned", "unassigned", "ambiguous", "failed")
    }
    payload = {
        "result_items": result_stats,
        "price_snapshots": snapshot_stats,
        "totals": totals,
    }
    conn.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
        (TASK_OWNED_DATA_MIGRATION_KEY, json.dumps(payload, sort_keys=True)),
    )
    print(
        "[DataOwnershipMigration] "
        f"assigned={totals['assigned']} unassigned={totals['unassigned']} "
        f"ambiguous={totals['ambiguous']} failed={totals['failed']}"
    )
    return payload


def migrate_task_owned_blacklist_rules(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
) -> dict[str, int]:
    """Copy filename rules to every task owner before legacy names are escaped."""
    empty_stats = {
        "task_rules_created": 0,
        "task_rules_merged": 0,
        "task_targets": 0,
        "legacy_rules_preserved": 0,
        "legacy_rules_moved": 0,
        "failed": 0,
    }
    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    ).fetchone()
    if marker is not None and not force:
        return empty_stats

    stats = dict(empty_stats)
    source_rows = conn.execute(
        """
        SELECT result_filename, blacklist_keywords_json, updated_at
        FROM result_blacklist_rules
        WHERE EXISTS (
            SELECT 1
            FROM result_items
            WHERE result_items.result_filename =
                  result_blacklist_rules.result_filename
              AND result_items.task_id IS NOT NULL
        )
        ORDER BY result_filename
        """
    ).fetchall()
    changed = False
    for source_row in source_rows:
        try:
            source_filename = str(source_row["result_filename"] or "")
            source_keywords = _decode_blacklist_keywords(
                source_row["blacklist_keywords_json"]
            )
            source_updated_at = str(source_row["updated_at"] or "")
            target_rows = conn.execute(
                """
                SELECT DISTINCT task_id
                FROM result_items
                WHERE result_filename = ? AND task_id IS NOT NULL
                ORDER BY task_id
                """,
                (source_filename,),
            ).fetchall()

            for target_row in target_rows:
                stats["task_targets"] += 1
                outcome = _merge_task_blacklist_rule(
                    conn,
                    task_id=int(target_row["task_id"]),
                    source_keywords=source_keywords,
                    source_updated_at=source_updated_at,
                )
                if outcome == "created":
                    stats["task_rules_created"] += 1
                    changed = True
                elif outcome == "changed":
                    stats["task_rules_merged"] += 1
                    changed = True

            has_unowned_rows = conn.execute(
                """
                SELECT 1
                FROM result_items
                WHERE result_filename = ? AND task_id IS NULL
                LIMIT 1
                """,
                (source_filename,),
            ).fetchone()
            if has_unowned_rows is not None or not target_rows:
                stats["legacy_rules_preserved"] += 1
                continue

            cursor = conn.execute(
                "DELETE FROM result_blacklist_rules WHERE result_filename = ?",
                (source_filename,),
            )
            stats["legacy_rules_moved"] += int(cursor.rowcount or 0)
            changed = changed or bool(cursor.rowcount)
        except Exception:
            stats["failed"] += 1
            raise

    marker_value = json.dumps(stats, sort_keys=True)
    existing_value = str(marker["value"]) if marker is not None else None
    existing_is_empty = marker is not None and all(
        value == 0 for value in _decode_migration_stats(existing_value).values()
    )
    if marker is None or changed or (source_rows and existing_is_empty):
        if existing_value != marker_value:
            conn.execute(
                "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
                (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY, marker_value),
            )
    return stats


def _merge_task_blacklist_rule(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    source_keywords: list[str],
    source_updated_at: str,
) -> str:
    target_row = conn.execute(
        """
        SELECT blacklist_keywords_json, updated_at
        FROM task_result_blacklist_rules
        WHERE task_id = ?
        """,
        (task_id,),
    ).fetchone()
    target_keywords = _decode_blacklist_keywords(
        target_row["blacklist_keywords_json"] if target_row else None
    )
    merged = normalize_blacklist_keywords([*target_keywords, *source_keywords])
    updated_at = max(
        source_updated_at,
        str(target_row["updated_at"] or "") if target_row else "",
    )
    if target_row is None:
        conn.execute(
            """
            INSERT INTO task_result_blacklist_rules (
                task_id, blacklist_keywords_json, updated_at
            ) VALUES (?, ?, ?)
            """,
            (task_id, json.dumps(merged, ensure_ascii=False), updated_at),
        )
        return "created"
    if merged == target_keywords and updated_at == str(target_row["updated_at"] or ""):
        return "no-op"
    conn.execute(
        """
        UPDATE task_result_blacklist_rules
        SET blacklist_keywords_json = ?, updated_at = ?
        WHERE task_id = ?
        """,
        (json.dumps(merged, ensure_ascii=False), updated_at, task_id),
    )
    return "changed"


def migrate_legacy_result_filename_namespace(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
) -> dict[str, int]:
    """Move unowned collision-prone filenames into the reserved legacy namespace."""
    marker = conn.execute(
        "SELECT 1 FROM app_metadata WHERE key = ?",
        (LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,),
    ).fetchone()
    if marker is not None and not force:
        return {"renamed_rows": 0, "renamed_rule_keys": 0}

    rows = conn.execute(
        """
        SELECT result_filename, keyword, COUNT(*) AS row_count
        FROM result_items
        WHERE task_id IS NULL
        GROUP BY result_filename, keyword
        ORDER BY result_filename, keyword
        """
    ).fetchall()
    targets_by_source: dict[str, set[str]] = {}
    renamed_rows = 0
    for row in rows:
        source = str(row["result_filename"] or "")
        keyword = str(row["keyword"] or "")
        target = build_legacy_result_filename(keyword)
        if source != build_result_filename(keyword) or source == target:
            continue
        cursor = conn.execute(
            """
            UPDATE result_items
            SET result_filename = ?
            WHERE task_id IS NULL AND result_filename = ? AND keyword = ?
            """,
            (target, source, keyword),
        )
        renamed_rows += int(cursor.rowcount or 0)
        targets_by_source.setdefault(source, set()).add(target)

    renamed_rule_keys, task_owned_rule_keys = _migrate_legacy_blacklist_rule_keys(
        conn,
        targets_by_source,
    )
    _record_namespace_blacklist_moves(conn, task_owned_rule_keys)
    payload = {
        "renamed_rows": renamed_rows,
        "renamed_rule_keys": renamed_rule_keys,
    }
    conn.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
        (
            LEGACY_RESULT_NAMESPACE_MIGRATION_KEY,
            json.dumps(payload, sort_keys=True),
        ),
    )
    return payload


def _migrate_legacy_blacklist_rule_keys(
    conn: sqlite3.Connection,
    targets_by_source: dict[str, set[str]],
) -> tuple[int, int]:
    migrated = 0
    task_owned_migrated = 0
    for source, targets in targets_by_source.items():
        source_row = conn.execute(
            """
            SELECT blacklist_keywords_json, updated_at
            FROM result_blacklist_rules
            WHERE result_filename = ?
            """,
            (source,),
        ).fetchone()
        if source_row is None:
            continue
        source_keywords = _decode_blacklist_keywords(
            source_row["blacklist_keywords_json"]
        )
        source_updated_at = str(source_row["updated_at"] or "")
        for target in sorted(targets):
            target_row = conn.execute(
                """
                SELECT blacklist_keywords_json, updated_at
                FROM result_blacklist_rules
                WHERE result_filename = ?
                """,
                (target,),
            ).fetchone()
            target_keywords = _decode_blacklist_keywords(
                target_row["blacklist_keywords_json"] if target_row else None
            )
            merged = normalize_blacklist_keywords(
                [*target_keywords, *source_keywords]
            )
            updated_at = max(
                source_updated_at,
                str(target_row["updated_at"] or "") if target_row else "",
            )
            conn.execute(
                """
                INSERT INTO result_blacklist_rules (
                    result_filename, blacklist_keywords_json, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(result_filename) DO UPDATE SET
                    blacklist_keywords_json = excluded.blacklist_keywords_json,
                    updated_at = excluded.updated_at
                """,
                (target, json.dumps(merged, ensure_ascii=False), updated_at),
            )
        source_has_unowned_rows = conn.execute(
            """
            SELECT 1
            FROM result_items
            WHERE result_filename = ? AND task_id IS NULL
            LIMIT 1
            """,
            (source,),
        ).fetchone()
        if source_has_unowned_rows is None:
            source_has_task_owned_rows = conn.execute(
                """
                SELECT 1
                FROM result_items
                WHERE result_filename = ? AND task_id IS NOT NULL
                LIMIT 1
                """,
                (source,),
            ).fetchone()
            conn.execute(
                "DELETE FROM result_blacklist_rules WHERE result_filename = ?",
                (source,),
            )
            migrated += 1
            task_owned_migrated += int(source_has_task_owned_rows is not None)
    return migrated, task_owned_migrated


def _record_namespace_blacklist_moves(
    conn: sqlite3.Connection,
    moved_count: int,
) -> None:
    if moved_count <= 0:
        return
    marker = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,),
    ).fetchone()
    if marker is None:
        return
    stats = _decode_migration_stats(marker["value"])
    adjusted = min(moved_count, stats.get("legacy_rules_preserved", 0))
    if adjusted <= 0:
        return
    stats["legacy_rules_preserved"] -= adjusted
    stats["legacy_rules_moved"] += adjusted
    conn.execute(
        "UPDATE app_metadata SET value = ? WHERE key = ?",
        (
            json.dumps(stats, sort_keys=True),
            TASK_OWNED_BLACKLIST_RULES_MIGRATION_KEY,
        ),
    )


def _decode_migration_stats(raw_value) -> dict[str, int]:
    try:
        decoded = json.loads(str(raw_value or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        str(key): int(value)
        for key, value in decoded.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _decode_blacklist_keywords(raw_value) -> list[str]:
    try:
        decoded = json.loads(str(raw_value or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(decoded, (list, str)):
        return []
    return normalize_blacklist_keywords(decoded)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _has_table_unique_constraint(
    conn: sqlite3.Connection,
    table_name: str,
    expected_columns: tuple[str, ...],
) -> bool:
    """Detect a UNIQUE table constraint by its SQLite autoindex columns."""
    quoted_table = '"' + table_name.replace('"', '""') + '"'
    for index_row in conn.execute(f"PRAGMA index_list({quoted_table})").fetchall():
        if str(index_row["origin"]) != "u":
            continue
        index_name = str(index_row["name"])
        quoted_index = '"' + index_name.replace('"', '""') + '"'
        actual_columns = tuple(
            str(column_row["name"])
            for column_row in conn.execute(
                f"PRAGMA index_info({quoted_index})"
            ).fetchall()
        )
        if actual_columns == expected_columns:
            return True
    return False


def _table_sequence(conn: sqlite3.Connection, table_name: str) -> int | None:
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?",
        (table_name,),
    ).fetchone()
    return int(row["seq"]) if row is not None else None


def _preserve_table_sequence(
    conn: sqlite3.Connection,
    table_name: str,
    previous_sequence: int | None,
) -> None:
    row = conn.execute(f"SELECT MAX(id) AS max_id FROM {table_name}").fetchone()
    max_id = int(row["max_id"]) if row and row["max_id"] is not None else None
    target = max(
        value for value in (previous_sequence, max_id, 0) if value is not None
    )
    current = _table_sequence(conn, table_name)
    if current is None:
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
            (table_name, target),
        )
    elif target > current:
        conn.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
            (target, table_name),
        )


def _rebuild_result_items_for_task_ownership(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "result_items")
    old_unique = _has_table_unique_constraint(
        conn,
        "result_items",
        LEGACY_RESULT_ITEMS_UNIQUE,
    )
    if "task_id" in columns and not old_unique:
        return False

    before_count = int(conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0])
    previous_sequence = _table_sequence(conn, "result_items")
    conn.execute("DROP TABLE IF EXISTS result_items__task_owner_migration")
    conn.execute(
        """
        CREATE TABLE result_items__task_owner_migration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
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
            search_text TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL
        )
        """
    )
    task_id_expression = "task_id" if "task_id" in columns else "NULL"
    _copy_result_items_for_task_ownership(conn, task_id_expression)
    conn.execute("DROP TABLE result_items")
    conn.execute(
        "ALTER TABLE result_items__task_owner_migration RENAME TO result_items"
    )
    after_count = int(conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0])
    if after_count != before_count:
        raise RuntimeError("result_items migration row count mismatch")
    _preserve_table_sequence(conn, "result_items", previous_sequence)
    return True


def _copy_result_items_for_task_ownership(
    conn: sqlite3.Connection,
    task_id_expression: str,
) -> None:
    source_columns = _table_columns(conn, "result_items")
    search_text_expression = (
        "COALESCE(search_text, '')" if "search_text" in source_columns else "''"
    )
    conn.execute(
        f"""
        INSERT INTO result_items__task_owner_migration (
            id, task_id, result_filename, keyword, task_name, crawl_time,
            publish_time, price, price_display, item_id, title, link,
            link_unique_key, seller_nickname, is_recommended, analysis_source,
            keyword_hit_count, status, search_text, raw_json
        )
        SELECT id, {task_id_expression}, result_filename, keyword, task_name,
               crawl_time, publish_time, price, price_display, item_id, title,
               link, link_unique_key, seller_nickname, is_recommended,
               analysis_source, keyword_hit_count, status,
               {search_text_expression}, raw_json
        FROM result_items
        """
    )


def _rebuild_price_snapshots_for_task_ownership(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "price_snapshots")
    old_unique = _has_table_unique_constraint(
        conn,
        "price_snapshots",
        LEGACY_PRICE_SNAPSHOTS_UNIQUE,
    )
    if "task_id" in columns and not old_unique:
        return False

    before_count = int(conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0])
    previous_sequence = _table_sequence(conn, "price_snapshots")
    conn.execute("DROP TABLE IF EXISTS price_snapshots__task_owner_migration")
    conn.execute(
        """
        CREATE TABLE price_snapshots__task_owner_migration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
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
            link TEXT
        )
        """
    )
    task_id_expression = "task_id" if "task_id" in columns else "NULL"
    _copy_price_snapshots_for_task_ownership(conn, task_id_expression)
    conn.execute("DROP TABLE price_snapshots")
    conn.execute(
        "ALTER TABLE price_snapshots__task_owner_migration RENAME TO price_snapshots"
    )
    after_count = int(conn.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0])
    if after_count != before_count:
        raise RuntimeError("price_snapshots migration row count mismatch")
    _preserve_table_sequence(conn, "price_snapshots", previous_sequence)
    return True


def _copy_price_snapshots_for_task_ownership(
    conn: sqlite3.Connection,
    task_id_expression: str,
) -> None:
    conn.execute(
        f"""
        INSERT INTO price_snapshots__task_owner_migration (
            id, task_id, keyword_slug, keyword, task_name, snapshot_time,
            snapshot_day, run_id, item_id, title, price, price_display,
            tags_json, region, seller, publish_time, link
        )
        SELECT id, {task_id_expression}, keyword_slug, keyword, task_name,
               snapshot_time, snapshot_day, run_id, item_id, title, price,
               price_display, tags_json, region, seller, publish_time, link
        FROM price_snapshots
        """
    )


def _ensure_task_owned_indexes(conn: sqlite3.Connection) -> None:
    statements = (
        ("idx_results_filename_crawl", "CREATE INDEX idx_results_filename_crawl ON result_items(result_filename, crawl_time DESC)"),
        ("idx_results_filename_publish", "CREATE INDEX idx_results_filename_publish ON result_items(result_filename, publish_time DESC)"),
        ("idx_results_filename_price", "CREATE INDEX idx_results_filename_price ON result_items(result_filename, price DESC)"),
        ("idx_results_filename_recommended", "CREATE INDEX idx_results_filename_recommended ON result_items(result_filename, is_recommended, analysis_source, crawl_time DESC)"),
        (RESULT_STATUS_INDEX_NAME, f"CREATE INDEX {RESULT_STATUS_INDEX_NAME} ON result_items(result_filename, status, crawl_time DESC)"),
        ("idx_result_items_task_link_unique", "CREATE UNIQUE INDEX idx_result_items_task_link_unique ON result_items(task_id, link_unique_key) WHERE task_id IS NOT NULL"),
        ("idx_result_items_legacy_file_link_unique", "CREATE UNIQUE INDEX idx_result_items_legacy_file_link_unique ON result_items(result_filename, link_unique_key) WHERE task_id IS NULL"),
        ("idx_result_items_task_crawl", "CREATE INDEX idx_result_items_task_crawl ON result_items(task_id, crawl_time DESC)"),
        *PAGINATION_INDEX_STATEMENTS,
        ("idx_snapshots_keyword_time", "CREATE INDEX idx_snapshots_keyword_time ON price_snapshots(keyword_slug, snapshot_time DESC)"),
        ("idx_snapshots_keyword_item_time", "CREATE INDEX idx_snapshots_keyword_item_time ON price_snapshots(keyword_slug, item_id, snapshot_time DESC)"),
        ("idx_price_snapshots_task_run_item_unique", "CREATE UNIQUE INDEX idx_price_snapshots_task_run_item_unique ON price_snapshots(task_id, run_id, item_id) WHERE task_id IS NOT NULL"),
        ("idx_price_snapshots_legacy_run_item_unique", "CREATE UNIQUE INDEX idx_price_snapshots_legacy_run_item_unique ON price_snapshots(keyword_slug, run_id, item_id) WHERE task_id IS NULL"),
        ("idx_price_snapshots_task_time", "CREATE INDEX idx_price_snapshots_task_time ON price_snapshots(task_id, snapshot_time DESC)"),
    )
    existing = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    for name, statement in statements:
        if name not in existing:
            conn.execute(statement)


def _assign_legacy_rows_to_tasks(
    conn: sqlite3.Connection,
    table_name: str,
) -> dict[str, int]:
    stats = {"assigned": 0, "unassigned": 0, "ambiguous": 0, "failed": 0}
    rows = conn.execute(
        f"SELECT id, task_name, keyword FROM {table_name} "
        "WHERE task_id IS NULL ORDER BY id"
    ).fetchall()
    for row in rows:
        task_name = str(row["task_name"] or "").strip()
        keyword = str(row["keyword"] or "").strip()
        if not task_name or not keyword:
            stats["unassigned"] += 1
            continue
        matches = conn.execute(
            "SELECT id FROM tasks WHERE task_name = ? AND keyword = ? ORDER BY id",
            (task_name, keyword),
        ).fetchall()
        if not matches:
            stats["unassigned"] += 1
            continue
        if len(matches) != 1:
            stats["ambiguous"] += 1
            continue
        try:
            conn.execute(
                f"UPDATE {table_name} SET task_id = ? WHERE id = ? AND task_id IS NULL",
                (int(matches[0]["id"]), int(row["id"])),
            )
            stats["assigned"] += 1
        except sqlite3.IntegrityError:
            stats["failed"] += 1
    return stats


@contextmanager
def sqlite_connection(
    db_path: str | None = None,
    *,
    read_only: bool = False,
) -> Iterator[sqlite3.Connection]:
    path = db_path or get_database_path()
    if read_only:
        database_uri = f"{Path(path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(database_uri, uri=True)
    else:
        _prepare_database_file(path)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _register_sqlite_functions(conn)
        if read_only:
            _apply_read_only_pragmas(conn)
        else:
            _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()
