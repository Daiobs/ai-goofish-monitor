"""
SQLite 持久化相关的统一命名规则。
"""
from __future__ import annotations

import re


DEFAULT_DATABASE_PATH = "data/app.sqlite3"
RESULT_FILE_SUFFIX = "_full_data.jsonl"
TASK_RESULT_FILENAME_PATTERN = re.compile(r"^task_(0|[1-9]\d*)_full_data\.jsonl$")


def build_result_filename(keyword: str) -> str:
    return f"{str(keyword or '').replace(' ', '_')}{RESULT_FILE_SUFFIX}"


def build_task_result_filename(task_id: int) -> str:
    if isinstance(task_id, bool):
        raise ValueError("task_id must be a non-negative integer")
    try:
        normalized = int(task_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("task_id must be a non-negative integer") from exc
    if normalized < 0:
        raise ValueError("task_id must be a non-negative integer")
    return f"task_{normalized}{RESULT_FILE_SUFFIX}"


def parse_task_result_filename(filename: str) -> int:
    text = str(filename or "")
    if "/" in text or "\\" in text or ".." in text:
        raise ValueError("invalid task result filename")
    match = TASK_RESULT_FILENAME_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError("invalid task result filename")
    return int(match.group(1))


def try_parse_task_result_filename(filename: str) -> int | None:
    text = str(filename or "")
    if re.match(r"^task_\d", text) is None:
        return None
    return parse_task_result_filename(text)


def normalize_keyword_from_filename(filename: str) -> str:
    return str(filename or "").replace(RESULT_FILE_SUFFIX, "")


def normalize_keyword_slug(keyword: str) -> str:
    text = "".join(
        char for char in str(keyword or "").lower().replace(" ", "_")
        if char.isalnum() or char in "_-"
    ).rstrip("_")
    return text or "unknown"
