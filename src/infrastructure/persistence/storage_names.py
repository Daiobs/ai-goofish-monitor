"""
SQLite 持久化相关的统一命名规则。
"""
from __future__ import annotations

import re


DEFAULT_DATABASE_PATH = "data/app.sqlite3"
RESULT_FILE_SUFFIX = "_full_data.jsonl"
LEGACY_RESULT_ESCAPE_PREFIX = "__legacy__"
TASK_RESULT_FILENAME_PATTERN = re.compile(r"^task_(0|[1-9]\d*)_full_data\.jsonl$")
TASK_RESULT_FILENAME_NAMESPACE_PATTERN = re.compile(r"^task_\d", re.IGNORECASE)
ESCAPED_LEGACY_RESULT_FILENAME_PATTERN = re.compile(
    rf"^{re.escape(LEGACY_RESULT_ESCAPE_PREFIX)}([0-9a-f]+)"
    rf"{re.escape(RESULT_FILE_SUFFIX)}$"
)


def build_result_filename(keyword: str) -> str:
    return f"{str(keyword or '').replace(' ', '_')}{RESULT_FILE_SUFFIX}"


def build_legacy_result_filename(keyword: str) -> str:
    raw_keyword = str(keyword or "")
    filename = build_result_filename(raw_keyword)
    if (
        TASK_RESULT_FILENAME_NAMESPACE_PATTERN.match(filename) is None
        and not filename.startswith(LEGACY_RESULT_ESCAPE_PREFIX)
    ):
        return filename
    encoded_keyword = raw_keyword.encode("utf-8").hex()
    return f"{LEGACY_RESULT_ESCAPE_PREFIX}{encoded_keyword}{RESULT_FILE_SUFFIX}"


def decode_legacy_result_filename(filename: str) -> str | None:
    match = ESCAPED_LEGACY_RESULT_FILENAME_PATTERN.fullmatch(str(filename or ""))
    if match is None:
        return None
    try:
        return bytes.fromhex(match.group(1)).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None


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
    if decode_legacy_result_filename(text) is not None:
        return None
    if TASK_RESULT_FILENAME_NAMESPACE_PATTERN.match(text) is None:
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
