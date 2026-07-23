"""
结果记录富化与文件名校验服务
"""

from src.infrastructure.persistence.storage_names import (
    normalize_keyword_from_filename,
    try_parse_task_result_filename,
)
from src.services.price_history_service import (
    build_item_price_context,
    load_price_snapshots,
    load_task_price_snapshots,
    parse_price_value,
)
from src.services.result_storage_service import (
    load_legacy_result_keyword,
    load_task_market_comparison_scope,
    load_visible_result_item_ids,
)


def validate_result_filename(filename: str) -> None:
    if (
        not filename.endswith(".jsonl")
        or "/" in filename
        or "\\" in filename
        or ".." in filename
    ):
        raise ValueError("无效的文件名")
    try_parse_task_result_filename(filename)


def enrich_records_with_price_insight(records: list[dict], filename: str) -> list[dict]:
    task_id = try_parse_task_result_filename(filename)
    if task_id is None:
        keyword = load_legacy_result_keyword(filename)
        snapshots = load_price_snapshots(keyword) if keyword is not None else []
    else:
        snapshots = load_task_price_snapshots(task_id)
    if not snapshots:
        return records

    if task_id is None:
        visible_item_ids = load_visible_result_item_ids(filename)
        comparable_item_ids = visible_item_ids
        market_scope = "legacy_keyword"
    else:
        comparison_scope = load_task_market_comparison_scope(task_id)
        visible_item_ids = comparison_scope["effective_item_ids"]
        comparable_item_ids = comparison_scope["comparable_item_ids"]
        market_scope = comparison_scope["scope_mode"]
    visible_snapshots = [
        snapshot
        for snapshot in snapshots
        if str(snapshot.get("item_id") or "") in visible_item_ids
    ]
    enriched = []
    for record in records:
        info = record.get("商品信息", {}) or {}
        clone = dict(record)
        clone["price_insight"] = build_item_price_context(
            snapshots,
            item_id=(item_id := str(info.get("商品ID") or "")),
            current_price=parse_price_value(info.get("当前售价")),
            market_snapshots=visible_snapshots,
            market_comparable=(
                True
                if task_id is None
                else item_id in comparable_item_ids
            ),
            market_scope=market_scope,
        )
        enriched.append(clone)
    return enriched
