"""Task-owned and legacy-compatible SQLite result storage."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime

from src.keyword_rule_engine import build_search_text, normalize_text
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.storage_names import (
    build_legacy_result_filename,
    build_task_result_filename,
    try_parse_task_result_filename,
)
from src.services.price_history_service import parse_price_value
from src.services.ai_request_compat import (
    TARGET_CATEGORY_NOT_TARGET,
    TARGET_CATEGORY_TARGET_BUNDLE,
    TARGET_CATEGORY_TARGET_ONLY,
    TARGET_CATEGORY_UNCERTAIN,
)
from src.services.result_blacklist_service import (
    is_valid_result_record_structure,
    match_blacklist_search_text,
    normalize_blacklist_keywords,
)


SORT_COLUMN_MAP = {
    "crawl_time": "crawl_time",
    "publish_time": "COALESCE(publish_time, '')",
    "price": "COALESCE(price, 0)",
    "keyword_hit_count": "keyword_hit_count",
}
VALID_ITEM_STATUSES = {"active", "hidden", "expired"}
DECISION_VIEW_WORTH_VIEWING = "worth_viewing"
DECISION_VIEW_COMPARABLE_TARGETS = "comparable_targets"
DECISION_VIEW_BUNDLES = "bundles"
DECISION_VIEW_EXCLUDED = "excluded"
DECISION_VIEW_AI_ISSUES = "ai_issues"
VALID_DECISION_VIEWS = frozenset(
    {
        DECISION_VIEW_WORTH_VIEWING,
        DECISION_VIEW_COMPARABLE_TARGETS,
        DECISION_VIEW_BUNDLES,
        DECISION_VIEW_EXCLUDED,
        DECISION_VIEW_AI_ISSUES,
    }
)

_DECISION_COMPARABLE_CLAUSE = (
    "record_valid = 1 "
    f"AND target_category = '{TARGET_CATEGORY_TARGET_ONLY}' "
    "AND market_comparable = 1 "
    "AND analysis_status = 'completed'"
)
_DECISION_EXCLUDED_CLAUSE = (
    "record_valid = 1 AND ("
    f"target_category IN ('{TARGET_CATEGORY_NOT_TARGET}', "
    f"'{TARGET_CATEGORY_UNCERTAIN}') "
    "OR market_comparable = 0)"
)
_DECISION_VIEW_CLAUSES = {
    DECISION_VIEW_WORTH_VIEWING: (
        f"{_DECISION_COMPARABLE_CLAUSE} AND is_recommended = 1"
    ),
    DECISION_VIEW_COMPARABLE_TARGETS: _DECISION_COMPARABLE_CLAUSE,
    DECISION_VIEW_BUNDLES: (
        "record_valid = 1 "
        f"AND target_category = '{TARGET_CATEGORY_TARGET_BUNDLE}'"
    ),
    DECISION_VIEW_EXCLUDED: _DECISION_EXCLUDED_CLAUSE,
    DECISION_VIEW_AI_ISSUES: (
        "record_valid = 1 "
        "AND analysis_status IN ('failed', 'skipped', 'pending')"
    ),
}


def _normalize_task_id(task_id: int) -> int:
    build_task_result_filename(task_id)
    return int(task_id)


def _get_link_unique_key(link: str) -> str:
    return link.split("&", 1)[0]


def _fallback_unique_key(record: dict, item: dict) -> str:
    item_id = str(item.get("商品ID") or "").strip()
    if item_id:
        return f"item:{item_id}"
    digest = hashlib.sha1(
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"hash:{digest}"


def _parse_raw_record(raw_json: str, *, status: str | None = None) -> dict:
    record = json.loads(raw_json)
    if not is_valid_result_record_structure(record):
        raise ValueError("result raw_json has an invalid record structure")
    if status is not None:
        record["_status"] = status
    return record


def _ownership_clause(
    *,
    task_id: int | None = None,
    filename: str | None = None,
) -> tuple[str, list, int | None]:
    if task_id is not None:
        normalized = _normalize_task_id(task_id)
        return "task_id = ?", [normalized], normalized
    if filename is None:
        raise ValueError("task_id or filename is required")
    parsed_task_id = try_parse_task_result_filename(filename)
    if parsed_task_id is not None:
        return "task_id = ?", [parsed_task_id], parsed_task_id
    return "task_id IS NULL AND result_filename = ?", [filename], None


def _build_query_conditions(
    *,
    task_id: int | None = None,
    filename: str | None = None,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
) -> tuple[str, list, int | None]:
    ownership, params, resolved_task_id = _ownership_clause(
        task_id=task_id,
        filename=filename,
    )
    conditions = [ownership]
    if ai_recommended_only:
        conditions.extend(("is_recommended = 1", "analysis_source = ?"))
        params.append("ai")
    if keyword_recommended_only:
        conditions.extend(("is_recommended = 1", "analysis_source = ?"))
        params.append("keyword")
    return " AND ".join(conditions), params, resolved_task_id


def _sort_expression(
    sort_by: str,
    sort_order: str,
    *,
    include_hidden: bool,
) -> str:
    column = SORT_COLUMN_MAP.get(sort_by, SORT_COLUMN_MAP["crawl_time"])
    direction = "ASC" if sort_order == "asc" else "DESC"
    status_order = (
        "(CASE WHEN status = 'active' THEN 0 ELSE 1 END), "
        if include_hidden
        else ""
    )
    return f"{status_order}{column} {direction}, id {direction}"


def _decode_blacklist_payload(row) -> list[str]:
    if row is None:
        return []
    try:
        payload = json.loads(row["blacklist_keywords_json"] or "[]")
    except json.JSONDecodeError:
        return []
    return normalize_blacklist_keywords(payload)


def _load_blacklist_keywords_from_conn(
    conn,
    *,
    filename: str | None = None,
    task_id: int | None = None,
) -> list[str]:
    _, _, resolved_task_id = _ownership_clause(task_id=task_id, filename=filename)
    if resolved_task_id is not None:
        row = conn.execute(
            "SELECT blacklist_keywords_json FROM task_result_blacklist_rules "
            "WHERE task_id = ?",
            (resolved_task_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT blacklist_keywords_json FROM result_blacklist_rules "
            "WHERE result_filename = ?",
            (filename,),
        ).fetchone()
    return _decode_blacklist_payload(row)


def _encode_blacklist_rules(keywords: list[str]) -> str:
    return json.dumps(keywords, ensure_ascii=False, separators=(",", ":"))


def _prepare_filtered_query(
    conn,
    *,
    filename: str | None,
    task_id: int | None,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    include_hidden: bool,
) -> tuple[str, list, int | None, list[str]]:
    where_clause, params, resolved_task_id = _build_query_conditions(
        task_id=task_id,
        filename=filename,
        ai_recommended_only=ai_recommended_only,
        keyword_recommended_only=keyword_recommended_only,
    )
    blacklist_keywords = _load_blacklist_keywords_from_conn(
        conn,
        task_id=resolved_task_id,
        filename=filename if resolved_task_id is None else None,
    )
    if not include_hidden:
        where_clause += (
            " AND status = ? "
            "AND result_blacklist_match(search_text, ?) = 0"
        )
        params.extend(("active", _encode_blacklist_rules(blacklist_keywords)))
    return where_clause, params, resolved_task_id, blacklist_keywords


def _decision_rows_cte(where_clause: str) -> str:
    return f"""
        WITH decision_rows AS (
            SELECT
                id,
                raw_json,
                status,
                search_text,
                price,
                crawl_time,
                item_id,
                CASE
                    WHEN json_valid(raw_json) = 1 THEN
                        CASE
                            WHEN json_type(raw_json, '$') = 'object'
                             AND COALESCE(
                                json_type(raw_json, '$."商品信息"'),
                                'null'
                             ) IN ('null', 'object')
                             AND COALESCE(
                                json_type(raw_json, '$."卖家信息"'),
                                'null'
                             ) IN ('null', 'object')
                             AND COALESCE(
                                json_type(raw_json, '$.ai_analysis'),
                                'null'
                             ) IN ('null', 'object')
                            THEN 1
                            ELSE 0
                        END
                    ELSE 0
                END AS record_valid,
                CASE
                    WHEN json_valid(raw_json) = 1
                    THEN json_extract(
                        raw_json,
                        '$.ai_analysis.target_category'
                    )
                END AS target_category,
                CASE
                    WHEN json_valid(raw_json) = 1
                    THEN json_extract(
                        raw_json,
                        '$.ai_analysis.market_comparable'
                    )
                END AS market_comparable,
                CASE
                    WHEN json_valid(raw_json) = 1
                    THEN json_extract(
                        raw_json,
                        '$.ai_analysis.analysis_status'
                    )
                END AS analysis_status,
                CASE
                    WHEN json_valid(raw_json) = 1 THEN
                        CASE
                            WHEN json_type(
                                raw_json,
                                '$.ai_analysis.is_recommended'
                            ) IN ('true', 'false')
                            THEN json_extract(
                                raw_json,
                                '$.ai_analysis.is_recommended'
                            )
                        END
                END AS is_recommended,
                CASE
                    WHEN json_valid(raw_json) = 1 THEN
                        CASE
                            WHEN json_type(
                                raw_json,
                                '$.ai_analysis.value_score'
                            ) IN ('integer', 'real')
                            THEN CAST(
                                json_extract(
                                    raw_json,
                                    '$.ai_analysis.value_score'
                                ) AS REAL
                            )
                        END
                END AS value_score
            FROM result_items
            WHERE {where_clause}
        )
    """


def _decision_view_clause(decision_view: str) -> str:
    try:
        return _DECISION_VIEW_CLAUSES[decision_view]
    except KeyError as exc:
        raise ValueError(f"unsupported decision view: {decision_view}") from exc


def _decision_summary_from_row(row) -> dict[str, int]:
    keys = (
        "all_count",
        "target_only_count",
        "target_bundle_count",
        "not_target_count",
        "uncertain_count",
        "comparable_count",
        "excluded_count",
        "ai_recommended_count",
        "ai_not_recommended_count",
        "ai_issue_count",
    )
    return {key: int(row[key] or 0) for key in keys}


def _decorate_record_visibility(
    record: dict,
    status: str | None,
    search_text: str,
    blacklist_keywords: list[str],
) -> dict:
    matched_keywords = match_blacklist_search_text(
        search_text,
        blacklist_keywords,
    )
    hidden_reason = None
    if status == "expired":
        hidden_reason = "expired"
    elif status and status != "active":
        hidden_reason = "manual"
    elif matched_keywords:
        hidden_reason = "rule"

    record["_status"] = status or "active"
    record["_matched_blacklist_keywords"] = matched_keywords
    record["_hidden_reason"] = hidden_reason
    record["_effective_hidden"] = hidden_reason is not None
    return record


def _parse_decorated_row(row, blacklist_keywords: list[str]) -> dict | None:
    try:
        record = _parse_raw_record(
            str(row["raw_json"]),
            status=row["status"],
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return _decorate_record_visibility(
        record,
        row["status"],
        str(row["search_text"] or ""),
        blacklist_keywords,
    )


def _load_filtered_records_from_conn(
    conn,
    *,
    filename: str | None = None,
    task_id: int | None = None,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool,
) -> list[dict]:
    where_clause, params, _, blacklist_keywords = _prepare_filtered_query(
        conn,
        filename=filename,
        task_id=task_id,
        ai_recommended_only=ai_recommended_only,
        keyword_recommended_only=keyword_recommended_only,
        include_hidden=include_hidden,
    )
    order_by = _sort_expression(
        sort_by,
        sort_order,
        include_hidden=include_hidden,
    )
    rows = conn.execute(
        f"SELECT raw_json, status, search_text FROM result_items "
        f"WHERE {where_clause} "
        f"ORDER BY {order_by}",
        tuple(params),
    ).fetchall()

    records: list[dict] = []
    for row in rows:
        decorated = _parse_decorated_row(row, blacklist_keywords)
        if decorated is not None:
            records.append(decorated)
    return records


def _save_result_record_sync(
    record: dict,
    keyword: str,
    task_id: int | None,
    *,
    update_existing: bool = False,
) -> bool:
    bootstrap_sqlite_storage()
    payload = copy.deepcopy(record)
    if task_id is not None:
        task_id = _normalize_task_id(task_id)
        payload["任务ID"] = task_id
        result_filename = build_task_result_filename(task_id)
    else:
        result_filename = build_legacy_result_filename(keyword)

    item = payload.get("商品信息", {}) or {}
    analysis = payload.get("ai_analysis", {}) or {}
    link = str(item.get("商品链接") or "")
    link_unique_key = (
        _get_link_unique_key(link) if link else _fallback_unique_key(payload, item)
    )
    try:
        keyword_hit_count = int(analysis.get("keyword_hit_count", 0))
    except (TypeError, ValueError):
        keyword_hit_count = 0
    search_text = normalize_text(build_search_text(payload))

    insert_values = (
        task_id,
        result_filename,
        payload.get("搜索关键字", keyword),
        payload.get("任务名称", ""),
        payload.get("爬取时间", ""),
        item.get("发布时间"),
        parse_price_value(item.get("当前售价")),
        item.get("当前售价"),
        item.get("商品ID"),
        item.get("商品标题"),
        link,
        link_unique_key,
        (payload.get("卖家信息", {}) or {}).get("卖家昵称")
        or item.get("卖家昵称"),
        1 if analysis.get("is_recommended") else 0,
        analysis.get("analysis_source"),
        keyword_hit_count,
        "active",
        search_text,
        json.dumps(payload, ensure_ascii=False),
    )

    with sqlite_connection() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO result_items (
                task_id, result_filename, keyword, task_name, crawl_time,
                publish_time, price, price_display, item_id, title, link,
                link_unique_key, seller_nickname, is_recommended,
                analysis_source, keyword_hit_count, status, search_text, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values,
        )
        persisted = int(cursor.rowcount or 0) > 0
        if not persisted and update_existing:
            update_values = (
                insert_values[2],
                insert_values[3],
                insert_values[4],
                insert_values[5],
                insert_values[6],
                insert_values[7],
                insert_values[8],
                insert_values[9],
                insert_values[10],
                insert_values[12],
                insert_values[13],
                insert_values[14],
                insert_values[15],
                insert_values[17],
                insert_values[18],
            )
            if task_id is None:
                cursor = conn.execute(
                    """
                    UPDATE result_items
                    SET keyword = ?, task_name = ?, crawl_time = ?,
                        publish_time = ?, price = ?, price_display = ?, item_id = ?,
                        title = ?, link = ?, seller_nickname = ?, is_recommended = ?,
                        analysis_source = ?, keyword_hit_count = ?, search_text = ?,
                        raw_json = ?
                    WHERE task_id IS NULL AND result_filename = ?
                      AND link_unique_key = ?
                    """,
                    (*update_values, result_filename, link_unique_key),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE result_items
                    SET keyword = ?, task_name = ?, crawl_time = ?,
                        publish_time = ?, price = ?, price_display = ?, item_id = ?,
                        title = ?, link = ?, seller_nickname = ?, is_recommended = ?,
                        analysis_source = ?, keyword_hit_count = ?, search_text = ?,
                        raw_json = ?
                    WHERE task_id = ? AND link_unique_key = ?
                    """,
                    (*update_values, task_id, link_unique_key),
                )
            persisted = int(cursor.rowcount or 0) > 0
        conn.commit()
    return persisted


async def save_result_record(record: dict, keyword: str) -> bool:
    """Write a legacy result with NULL task ownership."""
    return await asyncio.to_thread(_save_result_record_sync, record, keyword, None)


async def save_task_result_record(record: dict, keyword: str, task_id: int) -> bool:
    return await asyncio.to_thread(
        _save_result_record_sync,
        record,
        keyword,
        _normalize_task_id(task_id),
    )


async def upsert_result_record(record: dict, keyword: str) -> bool:
    """Insert or update a legacy row after its initial pending write."""
    return await asyncio.to_thread(
        _save_result_record_sync,
        record,
        keyword,
        None,
        update_existing=True,
    )


async def upsert_task_result_record(
    record: dict,
    keyword: str,
    task_id: int,
) -> bool:
    """Insert or update a task-owned row after its initial pending write."""
    return await asyncio.to_thread(
        _save_result_record_sync,
        record,
        keyword,
        _normalize_task_id(task_id),
        update_existing=True,
    )


def load_processed_link_keys(keyword: str) -> set[str]:
    """Load only legacy processed links for a keyword-owned config task."""
    bootstrap_sqlite_storage()
    filename = build_legacy_result_filename(keyword)
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT link_unique_key FROM result_items "
            "WHERE task_id IS NULL AND result_filename = ?",
            (filename,),
        ).fetchall()
    return {str(row["link_unique_key"]) for row in rows if row["link_unique_key"]}


def load_legacy_result_keyword(filename: str) -> str | None:
    """Read the keyword recorded for an exact legacy-owned result set."""
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT keyword FROM result_items "
            "WHERE task_id IS NULL AND result_filename = ? "
            "AND keyword IS NOT NULL AND keyword <> '' "
            "ORDER BY id DESC LIMIT 1",
            (filename,),
        ).fetchone()
    return str(row["keyword"]) if row is not None else None


def load_task_processed_link_keys(task_id: int) -> set[str]:
    bootstrap_sqlite_storage()
    normalized = _normalize_task_id(task_id)
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT link_unique_key FROM result_items WHERE task_id = ?",
            (normalized,),
        ).fetchall()
    return {str(row["link_unique_key"]) for row in rows if row["link_unique_key"]}


async def list_result_filenames() -> list[str]:
    return await asyncio.to_thread(_list_result_filenames_sync)


def _list_result_filenames_sync() -> list[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            """
            SELECT task_id, result_filename, MAX(crawl_time) AS latest_crawl_time
            FROM result_items
            GROUP BY task_id,
                     CASE WHEN task_id IS NULL THEN result_filename ELSE '' END
            ORDER BY latest_crawl_time DESC
            """
        ).fetchall()
    return [
        build_task_result_filename(int(row["task_id"]))
        if row["task_id"] is not None
        else str(row["result_filename"])
        for row in rows
    ]


def _result_exists_sync(*, filename: str | None = None, task_id: int | None = None) -> bool:
    bootstrap_sqlite_storage()
    clause, params, _ = _ownership_clause(task_id=task_id, filename=filename)
    with sqlite_connection() as conn:
        row = conn.execute(
            f"SELECT 1 FROM result_items WHERE {clause} LIMIT 1",
            tuple(params),
        ).fetchone()
    return row is not None


async def result_file_exists(filename: str) -> bool:
    return await asyncio.to_thread(_result_exists_sync, filename=filename)


async def task_result_records_exist(task_id: int) -> bool:
    return await asyncio.to_thread(_result_exists_sync, task_id=task_id)


def _delete_result_records_sync(
    *,
    filename: str | None = None,
    task_id: int | None = None,
) -> int:
    bootstrap_sqlite_storage()
    clause, params, _ = _ownership_clause(task_id=task_id, filename=filename)
    with sqlite_connection() as conn:
        cursor = conn.execute(
            f"DELETE FROM result_items WHERE {clause}",
            tuple(params),
        )
        conn.commit()
    return int(cursor.rowcount or 0)


async def delete_result_file_records(filename: str) -> int:
    return await asyncio.to_thread(_delete_result_records_sync, filename=filename)


async def delete_task_result_records(task_id: int) -> int:
    return await asyncio.to_thread(_delete_result_records_sync, task_id=task_id)


def _query_records_sync(
    *,
    filename: str | None,
    task_id: int | None,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    page: int,
    limit: int,
    include_hidden: bool,
) -> tuple[int, list[dict]]:
    bootstrap_sqlite_storage()
    safe_limit = max(int(limit), 0)
    with sqlite_connection() as conn:
        conn.execute("BEGIN")
        where_clause, params, _, blacklist_keywords = _prepare_filtered_query(
            conn,
            filename=filename,
            task_id=task_id,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            include_hidden=include_hidden,
        )
        total_row = conn.execute(
            f"SELECT COUNT(*) AS total FROM result_items WHERE {where_clause}",
            tuple(params),
        ).fetchone()
        total = int(total_row["total"] if total_row is not None else 0)
        if safe_limit <= 0 or total <= 0:
            return total, []

        page_index = max(int(page) - 1, 0)
        if page_index > (total - 1) // safe_limit:
            return total, []

        offset = page_index * safe_limit
        page_limit = min(safe_limit, total - offset)
        order_by = _sort_expression(
            sort_by,
            sort_order,
            include_hidden=include_hidden,
        )
        rows = conn.execute(
            f"SELECT raw_json, status, search_text FROM result_items "
            f"WHERE {where_clause} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*params, page_limit, offset),
        ).fetchall()

    records: list[dict] = []
    for row in rows:
        decorated = _parse_decorated_row(row, blacklist_keywords)
        if decorated is not None:
            records.append(decorated)
    return total, records


async def query_result_records(filename: str, **kwargs) -> tuple[int, list[dict]]:
    kwargs.setdefault("include_hidden", False)
    return await asyncio.to_thread(
        _query_records_sync,
        filename=filename,
        task_id=None,
        **kwargs,
    )


async def query_task_result_records(task_id: int, **kwargs) -> tuple[int, list[dict]]:
    kwargs.setdefault("include_hidden", False)
    return await asyncio.to_thread(
        _query_records_sync,
        filename=None,
        task_id=_normalize_task_id(task_id),
        **kwargs,
    )


def _query_task_decision_records_sync(
    *,
    task_id: int,
    decision_view: str,
    page: int,
    limit: int,
    include_hidden: bool,
) -> tuple[int, list[dict], dict[str, int]]:
    bootstrap_sqlite_storage()
    normalized_task_id = _normalize_task_id(task_id)
    view_clause = _decision_view_clause(decision_view)
    safe_limit = max(int(limit), 0)

    with sqlite_connection() as conn:
        conn.execute("BEGIN")
        where_clause, params, _, blacklist_keywords = _prepare_filtered_query(
            conn,
            filename=None,
            task_id=normalized_task_id,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            include_hidden=include_hidden,
        )
        cte = _decision_rows_cte(where_clause)
        aggregate = conn.execute(
            f"""
            {cte}
            SELECT
                COUNT(*) AS all_count,
                COALESCE(SUM(
                    CASE WHEN record_valid = 1
                           AND target_category = ?
                         THEN 1 ELSE 0 END
                ), 0) AS target_only_count,
                COALESCE(SUM(
                    CASE WHEN record_valid = 1
                           AND target_category = ?
                         THEN 1 ELSE 0 END
                ), 0) AS target_bundle_count,
                COALESCE(SUM(
                    CASE WHEN record_valid = 1
                           AND target_category = ?
                         THEN 1 ELSE 0 END
                ), 0) AS not_target_count,
                COALESCE(SUM(
                    CASE WHEN record_valid = 1
                           AND target_category = ?
                         THEN 1 ELSE 0 END
                ), 0) AS uncertain_count,
                COALESCE(SUM(
                    CASE WHEN {_DECISION_COMPARABLE_CLAUSE}
                         THEN 1 ELSE 0 END
                ), 0) AS comparable_count,
                COALESCE(SUM(
                    CASE WHEN {_DECISION_EXCLUDED_CLAUSE}
                         THEN 1 ELSE 0 END
                ), 0) AS excluded_count,
                COALESCE(SUM(
                    CASE WHEN record_valid = 1
                           AND analysis_status = 'completed'
                           AND is_recommended = 1
                         THEN 1 ELSE 0 END
                ), 0) AS ai_recommended_count,
                COALESCE(SUM(
                    CASE WHEN record_valid = 1
                           AND analysis_status = 'completed'
                           AND is_recommended = 0
                         THEN 1 ELSE 0 END
                ), 0) AS ai_not_recommended_count,
                COALESCE(SUM(
                    CASE WHEN record_valid = 1
                           AND analysis_status IN (
                               'failed',
                               'skipped',
                               'pending'
                           )
                         THEN 1 ELSE 0 END
                ), 0) AS ai_issue_count,
                COALESCE(SUM(
                    CASE WHEN {view_clause}
                         THEN 1 ELSE 0 END
                ), 0) AS current_view_count
            FROM decision_rows
            """,
            (
                *params,
                TARGET_CATEGORY_TARGET_ONLY,
                TARGET_CATEGORY_TARGET_BUNDLE,
                TARGET_CATEGORY_NOT_TARGET,
                TARGET_CATEGORY_UNCERTAIN,
            ),
        ).fetchone()
        summary = _decision_summary_from_row(aggregate)
        total = int(aggregate["current_view_count"] or 0)
        if safe_limit <= 0 or total <= 0:
            return total, [], summary

        page_index = max(int(page) - 1, 0)
        if page_index > (total - 1) // safe_limit:
            return total, [], summary
        offset = page_index * safe_limit
        page_limit = min(safe_limit, total - offset)

        median_row = conn.execute(
            f"""
            {cte},
            comparable_prices AS (
                SELECT
                    price,
                    ROW_NUMBER() OVER (ORDER BY price ASC) AS row_number,
                    COUNT(*) OVER () AS price_count
                FROM decision_rows
                WHERE {_DECISION_COMPARABLE_CLAUSE}
                  AND price IS NOT NULL
                  AND price > 0
            )
            SELECT AVG(price) AS median_price
            FROM comparable_prices
            WHERE row_number IN (
                (price_count + 1) / 2,
                (price_count + 2) / 2
            )
            """,
            tuple(params),
        ).fetchone()
        median_price = (
            float(median_row["median_price"])
            if median_row is not None and median_row["median_price"] is not None
            else None
        )
        if median_price is None:
            below_median_order = "0"
            page_params = (*params, page_limit, offset)
        else:
            below_median_order = (
                "CASE WHEN "
                f"{_DECISION_COMPARABLE_CLAUSE} "
                "AND price IS NOT NULL "
                "AND price > 0 "
                "AND price < ? "
                "THEN 1 ELSE 0 END"
            )
            page_params = (*params, median_price, page_limit, offset)

        rows = conn.execute(
            f"""
            {cte}
            SELECT raw_json, status, search_text
            FROM decision_rows
            WHERE {view_clause}
            ORDER BY
                COALESCE(is_recommended, 0) DESC,
                CASE WHEN {_DECISION_COMPARABLE_CLAUSE}
                     THEN 1 ELSE 0 END DESC,
                CASE WHEN value_score IS NOT NULL
                     THEN 1 ELSE 0 END DESC,
                value_score DESC,
                {below_median_order} DESC,
                COALESCE(crawl_time, '') DESC,
                COALESCE(NULLIF(TRIM(item_id), ''), '') ASC,
                id ASC
            LIMIT ? OFFSET ?
            """,
            page_params,
        ).fetchall()

    records: list[dict] = []
    for row in rows:
        decorated = _parse_decorated_row(row, blacklist_keywords)
        if decorated is not None:
            records.append(decorated)
    return total, records, summary


async def query_task_decision_records(
    task_id: int,
    *,
    decision_view: str,
    page: int,
    limit: int,
    include_hidden: bool = False,
) -> tuple[int, list[dict], dict[str, int]]:
    """Query one task-wide decision view using SQL filtering and pagination."""
    return await asyncio.to_thread(
        _query_task_decision_records_sync,
        task_id=_normalize_task_id(task_id),
        decision_view=decision_view,
        page=page,
        limit=limit,
        include_hidden=include_hidden,
    )


def _load_all_records_sync(
    *,
    filename: str | None,
    task_id: int | None,
    ai_recommended_only: bool,
    keyword_recommended_only: bool,
    sort_by: str,
    sort_order: str,
    include_hidden: bool,
) -> list[dict]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute("BEGIN")
        return _load_filtered_records_from_conn(
            conn,
            filename=filename,
            task_id=task_id,
            ai_recommended_only=ai_recommended_only,
            keyword_recommended_only=keyword_recommended_only,
            sort_by=sort_by,
            sort_order=sort_order,
            include_hidden=include_hidden,
        )


async def load_all_result_records(filename: str, **kwargs) -> list[dict]:
    kwargs.setdefault("include_hidden", False)
    return await asyncio.to_thread(
        _load_all_records_sync,
        filename=filename,
        task_id=None,
        **kwargs,
    )


async def load_all_task_result_records(task_id: int, **kwargs) -> list[dict]:
    kwargs.setdefault("include_hidden", False)
    return await asyncio.to_thread(
        _load_all_records_sync,
        filename=None,
        task_id=_normalize_task_id(task_id),
        **kwargs,
    )


def _build_ndjson_sync(
    *,
    filename: str | None = None,
    task_id: int | None = None,
) -> str:
    bootstrap_sqlite_storage()
    clause, params, _ = _ownership_clause(task_id=task_id, filename=filename)
    with sqlite_connection() as conn:
        rows = conn.execute(
            f"SELECT raw_json FROM result_items WHERE {clause} ORDER BY id ASC",
            tuple(params),
        ).fetchall()
    return "\n".join(str(row["raw_json"]) for row in rows)


async def build_result_ndjson(filename: str) -> str:
    return await asyncio.to_thread(_build_ndjson_sync, filename=filename)


async def build_task_result_ndjson(task_id: int) -> str:
    return await asyncio.to_thread(_build_ndjson_sync, task_id=task_id)


def _summarize_visible_records(visible_records: list[dict]) -> dict | None:
    if not visible_records:
        return None
    recommended = [
        record
        for record in visible_records
        if (record.get("ai_analysis", {}) or {}).get("is_recommended") is True
    ]
    ai_count = sum(
        1
        for record in recommended
        if (record.get("ai_analysis", {}) or {}).get("analysis_source") == "ai"
    )
    keyword_count = sum(
        1
        for record in recommended
        if (record.get("ai_analysis", {}) or {}).get("analysis_source") == "keyword"
    )
    return {
        "total_items": len(visible_records),
        "recommended_items": len(recommended),
        "ai_recommended_items": ai_count,
        "keyword_recommended_items": keyword_count,
        "latest_crawl_time": visible_records[0].get("爬取时间"),
        "latest_record": visible_records[0],
        "latest_recommendation": recommended[0] if recommended else None,
    }


def _load_summary_sync(
    *,
    filename: str | None = None,
    task_id: int | None = None,
) -> dict | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute("BEGIN")
        where_clause, params, _, blacklist_keywords = _prepare_filtered_query(
            conn,
            filename=filename,
            task_id=task_id,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            include_hidden=False,
        )
        aggregate = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_items,
                COALESCE(SUM(CASE WHEN is_recommended = 1 THEN 1 ELSE 0 END), 0)
                    AS recommended_items,
                COALESCE(SUM(CASE WHEN is_recommended = 1
                                   AND analysis_source = 'ai' THEN 1 ELSE 0 END), 0)
                    AS ai_recommended_items,
                COALESCE(SUM(CASE WHEN is_recommended = 1
                                   AND analysis_source = 'keyword' THEN 1 ELSE 0 END), 0)
                    AS keyword_recommended_items,
                MAX(crawl_time) AS latest_crawl_time
            FROM result_items
            WHERE {where_clause}
            """,
            tuple(params),
        ).fetchone()
        total_items = int(aggregate["total_items"] if aggregate else 0)
        if total_items == 0:
            return None

        order_by = _sort_expression(
            "crawl_time",
            "desc",
            include_hidden=False,
        )
        latest_row = conn.execute(
            f"SELECT raw_json, status, search_text FROM result_items "
            f"WHERE {where_clause} ORDER BY {order_by} LIMIT 1",
            tuple(params),
        ).fetchone()
        latest_recommendation_row = conn.execute(
            f"SELECT raw_json, status, search_text FROM result_items "
            f"WHERE {where_clause} AND is_recommended = 1 "
            f"ORDER BY {order_by} LIMIT 1",
            tuple(params),
        ).fetchone()

    latest_record = (
        _parse_decorated_row(latest_row, blacklist_keywords)
        if latest_row is not None
        else None
    )
    latest_recommendation = (
        _parse_decorated_row(latest_recommendation_row, blacklist_keywords)
        if latest_recommendation_row is not None
        else None
    )
    return {
        "total_items": total_items,
        "recommended_items": int(aggregate["recommended_items"]),
        "ai_recommended_items": int(aggregate["ai_recommended_items"]),
        "keyword_recommended_items": int(aggregate["keyword_recommended_items"]),
        "latest_crawl_time": aggregate["latest_crawl_time"],
        "latest_record": latest_record,
        "latest_recommendation": latest_recommendation,
    }


async def load_result_summary(filename: str) -> dict | None:
    return await asyncio.to_thread(_load_summary_sync, filename=filename)


async def load_task_result_summary(task_id: int) -> dict | None:
    return await asyncio.to_thread(_load_summary_sync, task_id=task_id)


def _update_item_status_sync(
    *,
    item_id: str,
    status: str,
    filename: str | None = None,
    task_id: int | None = None,
) -> bool:
    if status not in VALID_ITEM_STATUSES:
        raise ValueError(f"status must be one of {VALID_ITEM_STATUSES}")
    bootstrap_sqlite_storage()
    clause, params, _ = _ownership_clause(task_id=task_id, filename=filename)
    with sqlite_connection() as conn:
        cursor = conn.execute(
            f"UPDATE result_items SET status = ? WHERE {clause} AND item_id = ?",
            (status, *params, item_id),
        )
        conn.commit()
    return int(cursor.rowcount or 0) > 0


async def update_item_status(filename: str, item_id: str, status: str) -> bool:
    return await asyncio.to_thread(
        _update_item_status_sync,
        filename=filename,
        item_id=item_id,
        status=status,
    )


async def update_task_result_item_status(
    task_id: int,
    item_id: str,
    status: str,
) -> bool:
    return await asyncio.to_thread(
        _update_item_status_sync,
        task_id=task_id,
        item_id=item_id,
        status=status,
    )


def _load_blacklist_sync(
    *,
    filename: str | None = None,
    task_id: int | None = None,
) -> list[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        return _load_blacklist_keywords_from_conn(
            conn,
            filename=filename,
            task_id=task_id,
        )


async def load_result_blacklist_keywords(filename: str) -> list[str]:
    return await asyncio.to_thread(_load_blacklist_sync, filename=filename)


async def load_task_result_blacklist_keywords(task_id: int) -> list[str]:
    return await asyncio.to_thread(_load_blacklist_sync, task_id=task_id)


def _save_blacklist_sync(
    keywords: list[str],
    *,
    filename: str | None = None,
    task_id: int | None = None,
) -> list[str]:
    bootstrap_sqlite_storage()
    normalized = normalize_blacklist_keywords(keywords)
    now = datetime.now().isoformat()
    _, _, resolved_task_id = _ownership_clause(task_id=task_id, filename=filename)
    with sqlite_connection() as conn:
        if resolved_task_id is not None:
            conn.execute(
                """
                INSERT INTO task_result_blacklist_rules (
                    task_id, blacklist_keywords_json, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    blacklist_keywords_json = excluded.blacklist_keywords_json,
                    updated_at = excluded.updated_at
                """,
                (resolved_task_id, json.dumps(normalized, ensure_ascii=False), now),
            )
        else:
            conn.execute(
                """
                INSERT INTO result_blacklist_rules (
                    result_filename, blacklist_keywords_json, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(result_filename) DO UPDATE SET
                    blacklist_keywords_json = excluded.blacklist_keywords_json,
                    updated_at = excluded.updated_at
                """,
                (filename, json.dumps(normalized, ensure_ascii=False), now),
            )
        conn.commit()
    return normalized


async def save_result_blacklist_keywords(
    filename: str,
    keywords: list[str],
) -> list[str]:
    return await asyncio.to_thread(
        _save_blacklist_sync,
        keywords,
        filename=filename,
    )


async def save_task_result_blacklist_keywords(
    task_id: int,
    keywords: list[str],
) -> list[str]:
    return await asyncio.to_thread(
        _save_blacklist_sync,
        keywords,
        task_id=task_id,
    )


def delete_task_result_blacklist_rules(task_id: int) -> int:
    bootstrap_sqlite_storage()
    normalized = _normalize_task_id(task_id)
    with sqlite_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM task_result_blacklist_rules WHERE task_id = ?",
            (normalized,),
        )
        conn.commit()
    return int(cursor.rowcount or 0)


def _load_visible_item_ids_sync(
    *,
    filename: str | None = None,
    task_id: int | None = None,
) -> set[str]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute("BEGIN")
        where_clause, params, _, _ = _prepare_filtered_query(
            conn,
            filename=filename,
            task_id=task_id,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            include_hidden=False,
        )
        rows = conn.execute(
            f"SELECT item_id FROM result_items WHERE {where_clause} "
            "AND item_id IS NOT NULL AND TRIM(item_id) <> ''",
            tuple(params),
        ).fetchall()
    return {str(row["item_id"]).strip() for row in rows}


def load_visible_result_item_ids(filename: str) -> set[str]:
    return _load_visible_item_ids_sync(filename=filename)


def load_visible_task_result_item_ids(task_id: int) -> set[str]:
    return _load_visible_item_ids_sync(task_id=task_id)


def load_task_market_comparison_scope(task_id: int) -> dict:
    """Return visible task items grouped by AI market-comparability semantics."""
    bootstrap_sqlite_storage()
    normalized_task_id = _normalize_task_id(task_id)
    with sqlite_connection() as conn:
        conn.execute("BEGIN")
        where_clause, params, _, _ = _prepare_filtered_query(
            conn,
            filename=None,
            task_id=normalized_task_id,
            ai_recommended_only=False,
            keyword_recommended_only=False,
            include_hidden=False,
        )
        rows = conn.execute(
            f"""
            SELECT
                item_id,
                CASE
                    WHEN json_valid(raw_json) = 1 THEN
                        CASE
                            WHEN json_type(raw_json, '$') = 'object'
                             AND COALESCE(
                                json_type(raw_json, '$."商品信息"'),
                                'null'
                             ) IN ('null', 'object')
                             AND COALESCE(
                                json_type(raw_json, '$."卖家信息"'),
                                'null'
                             ) IN ('null', 'object')
                             AND COALESCE(
                                json_type(raw_json, '$.ai_analysis'),
                                'null'
                             ) IN ('null', 'object')
                            THEN 1
                            ELSE 0
                        END
                    ELSE 0
                END AS record_valid,
                CASE
                    WHEN json_valid(raw_json) = 1
                    THEN json_extract(
                        raw_json,
                        '$.ai_analysis.target_category'
                    )
                END AS target_category,
                CASE
                    WHEN json_valid(raw_json) = 1
                    THEN json_extract(
                        raw_json,
                        '$.ai_analysis.market_comparable'
                    )
                END AS market_comparable,
                CASE
                    WHEN json_valid(raw_json) = 1
                    THEN json_extract(
                        raw_json,
                        '$.ai_analysis.analysis_status'
                    )
                END AS analysis_status,
                CASE
                    WHEN json_valid(raw_json) = 1
                    THEN json_extract(
                        raw_json,
                        '$.ai_analysis.analysis_source'
                    )
                END AS analysis_source
            FROM result_items
            WHERE {where_clause}
              AND item_id IS NOT NULL
              AND TRIM(item_id) <> ''
            """,
            tuple(params),
        ).fetchall()

    visible_item_ids: set[str] = set()
    comparable_item_ids: set[str] = set()
    keyword_item_ids: set[str] = set()
    counts = {
        TARGET_CATEGORY_TARGET_ONLY: 0,
        TARGET_CATEGORY_TARGET_BUNDLE: 0,
        TARGET_CATEGORY_NOT_TARGET: 0,
        TARGET_CATEGORY_UNCERTAIN: 0,
        "unclassified": 0,
    }

    for row in rows:
        item_id = str(row["item_id"]).strip()
        visible_item_ids.add(item_id)
        if int(row["record_valid"] or 0) != 1:
            counts["unclassified"] += 1
            continue

        category = row["target_category"]
        analysis_source = str(row["analysis_source"] or "")
        if analysis_source == "keyword" and category is None:
            keyword_item_ids.add(item_id)
            continue
        if not isinstance(category, str) or category not in counts:
            counts["unclassified"] += 1
            continue
        counts[str(category)] += 1
        if (
            category == TARGET_CATEGORY_TARGET_ONLY
            and row["market_comparable"] == 1
            and row["analysis_status"] == "completed"
        ):
            comparable_item_ids.add(item_id)

    classified_count = sum(
        counts[category]
        for category in (
            TARGET_CATEGORY_TARGET_ONLY,
            TARGET_CATEGORY_TARGET_BUNDLE,
            TARGET_CATEGORY_NOT_TARGET,
            TARGET_CATEGORY_UNCERTAIN,
        )
    )
    classification_available = classified_count > 0
    keyword_only = (
        bool(visible_item_ids)
        and keyword_item_ids == visible_item_ids
        and classified_count == 0
        and counts["unclassified"] == 0
    )
    scope_mode = "keyword_all_visible" if keyword_only else "ai_classified"
    if keyword_only:
        comparable_item_ids = set(visible_item_ids)
    effective_item_ids = set(comparable_item_ids)
    return {
        "visible_item_ids": visible_item_ids,
        "comparable_item_ids": comparable_item_ids,
        "effective_item_ids": effective_item_ids,
        "classification_available": classification_available,
        "scope_mode": scope_mode,
        "visible_count": len(visible_item_ids),
        "classified_count": classified_count,
        "comparable_count": len(comparable_item_ids),
        "excluded_count": len(visible_item_ids) - len(comparable_item_ids),
        "target_only_count": counts[TARGET_CATEGORY_TARGET_ONLY],
        "target_bundle_count": counts[TARGET_CATEGORY_TARGET_BUNDLE],
        "not_target_count": counts[TARGET_CATEGORY_NOT_TARGET],
        "uncertain_count": counts[TARGET_CATEGORY_UNCERTAIN],
        "unclassified_count": counts["unclassified"],
    }
