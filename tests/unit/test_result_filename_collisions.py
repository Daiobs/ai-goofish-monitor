import pytest

from src.infrastructure.persistence.storage_names import (
    LEGACY_RESULT_ESCAPE_PREFIX,
    build_legacy_result_filename,
    build_result_filename,
    build_task_result_filename,
    decode_legacy_result_filename,
    parse_task_result_filename,
    try_parse_task_result_filename,
)
from src.services.result_file_service import validate_result_filename


def test_legacy_filename_generator_escapes_canonical_namespace_reversibly():
    keyword = "task_42"
    canonical = build_task_result_filename(42)
    escaped = build_legacy_result_filename(keyword)

    assert build_result_filename(keyword) == canonical
    assert escaped.startswith(LEGACY_RESULT_ESCAPE_PREFIX)
    assert escaped != canonical
    assert decode_legacy_result_filename(escaped) == keyword
    assert try_parse_task_result_filename(escaped) is None
    validate_result_filename(escaped)


def test_legacy_filename_generator_preserves_ordinary_names_and_escapes_prefix():
    assert build_legacy_result_filename("camera lens") == "camera_lens_full_data.jsonl"
    assert decode_legacy_result_filename("camera_lens_full_data.jsonl") is None

    keyword = f"{LEGACY_RESULT_ESCAPE_PREFIX}7461736b"
    escaped = build_legacy_result_filename(keyword)
    assert escaped != build_result_filename(keyword)
    assert decode_legacy_result_filename(escaped) == keyword

    mixed_case = build_legacy_result_filename("Task_42")
    assert mixed_case != "Task_42_full_data.jsonl"
    assert decode_legacy_result_filename(mixed_case) == "Task_42"


def test_malformed_canonical_names_remain_rejected_but_generate_safely_for_legacy():
    malformed = "task_042_full_data.jsonl"
    with pytest.raises(ValueError):
        parse_task_result_filename(malformed)
    with pytest.raises(ValueError):
        validate_result_filename(malformed)

    escaped = build_legacy_result_filename("task 042")
    assert escaped != malformed
    assert decode_legacy_result_filename(escaped) == "task 042"
    validate_result_filename(escaped)

    for traversal in (
        "../task_42_full_data.jsonl",
        "folder/task_42_full_data.jsonl",
        r"folder\task_42_full_data.jsonl",
    ):
        with pytest.raises(ValueError):
            validate_result_filename(traversal)
