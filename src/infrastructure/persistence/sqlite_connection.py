"""
SQLite 连接与 schema 初始化。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.infrastructure.persistence.storage_names import DEFAULT_DATABASE_PATH


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
    """,
    """
    CREATE TABLE IF NOT EXISTS price_snapshots (
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
    """,
    """
    CREATE TABLE IF NOT EXISTS result_blacklist_rules (
        result_filename TEXT PRIMARY KEY,
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
REQUIRED_TABLES = {
    "app_metadata",
    "tasks",
    "result_items",
    "price_snapshots",
    "result_blacklist_rules",
    "auth_sessions",
}
REQUIRED_INDEXES = {
    "idx_tasks_name",
    "idx_results_filename_crawl",
    "idx_results_filename_publish",
    "idx_results_filename_price",
    "idx_results_filename_recommended",
    "idx_results_filename_status_crawl",
    "idx_snapshots_keyword_time",
    "idx_snapshots_keyword_item_time",
    "idx_auth_sessions_expires",
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
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        _migrate_tasks_autoincrement(conn)
        _migrate_result_items_status(conn)
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
    if "status" not in result_columns:
        return False

    migration_rows = conn.execute(
        "SELECT key FROM app_metadata WHERE key IN (?, ?)",
        (TASK_IDENTITY_MIGRATION_KEY, RESULT_STATUS_MIGRATION_KEY),
    ).fetchall()
    completed = {str(row["key"]) for row in migration_rows}
    return completed == {TASK_IDENTITY_MIGRATION_KEY, RESULT_STATUS_MIGRATION_KEY}


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
    """为 result_items 表添加 status 列（仅执行一次）。"""
    row = conn.execute(
        "SELECT value FROM app_metadata WHERE key = ?",
        (RESULT_STATUS_MIGRATION_KEY,),
    ).fetchone()
    if row is not None:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(result_items)").fetchall()]
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE result_items ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    conn.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, 'done')",
        (RESULT_STATUS_MIGRATION_KEY,),
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_results_filename_status_crawl"
        " ON result_items(result_filename, status, crawl_time DESC)"
    )


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
        if read_only:
            _apply_read_only_pragmas(conn)
        else:
            _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()
