from __future__ import annotations

import json
from datetime import datetime, timedelta

from src.failure_guard import FailureGuard, canonical_task_key


def test_failure_guard_opens_circuit_after_threshold_and_rate_limits(tmp_path):
    guard_path = tmp_path / "guard.json"
    cookie_path = tmp_path / "xianyu_state.json"
    cookie_path.write_text("{}", encoding="utf-8")

    guard = FailureGuard(
        path=str(guard_path),
        threshold=3,
        pause_seconds=3 * 24 * 60 * 60,
        tz_name="Asia/Shanghai",
    )

    base = datetime(2026, 3, 4, 12, 0, 0)

    r1 = guard.record_failure("task-a", "err-1", cookie_path=str(cookie_path), now=base)
    assert r1["should_notify"] is False
    assert r1["opened_circuit"] is False

    r2 = guard.record_failure("task-a", "err-2", cookie_path=str(cookie_path), now=base)
    assert r2["should_notify"] is False
    assert r2["opened_circuit"] is False

    r3 = guard.record_failure("task-a", "err-3", cookie_path=str(cookie_path), now=base)
    assert r3["should_notify"] is True
    assert r3["opened_circuit"] is True
    assert r3["paused_until"] is not None

    d0 = guard.should_skip_start("task-a", cookie_path=str(cookie_path), now=base)
    assert d0.skip is True
    assert d0.should_notify is False

    next_day = base + timedelta(days=1, minutes=1)
    d1 = guard.should_skip_start("task-a", cookie_path=str(cookie_path), now=next_day)
    assert d1.skip is True
    assert d1.should_notify is True

    d1b = guard.should_skip_start("task-a", cookie_path=str(cookie_path), now=next_day)
    assert d1b.skip is True
    assert d1b.should_notify is False


def test_failure_guard_auto_recovers_on_cookie_change(tmp_path):
    guard_path = tmp_path / "guard.json"
    cookie_path = tmp_path / "xianyu_state.json"
    cookie_path.write_text("{}", encoding="utf-8")

    guard = FailureGuard(
        path=str(guard_path),
        threshold=2,
        pause_seconds=3 * 24 * 60 * 60,
        tz_name="Asia/Shanghai",
    )

    base = datetime(2026, 3, 4, 12, 0, 0)

    guard.record_failure("task-a", "err-1", cookie_path=str(cookie_path), now=base)
    guard.record_failure("task-a", "err-2", cookie_path=str(cookie_path), now=base)

    paused = guard.should_skip_start("task-a", cookie_path=str(cookie_path), now=base)
    assert paused.skip is True

    cookie_path.write_text('{"updated": true}', encoding="utf-8")

    recovered = guard.should_skip_start(
        "task-a",
        cookie_path=str(cookie_path),
        now=base + timedelta(minutes=1),
    )
    assert recovered.skip is False


def test_same_name_tasks_keep_failure_state_isolated_by_id(tmp_path):
    guard = FailureGuard(
        path=str(tmp_path / "guard.json"),
        threshold=1,
        pause_seconds=3600,
    )
    now = datetime(2026, 3, 4, 12, 0, 0)
    first_key = canonical_task_key(10)
    second_key = canonical_task_key(11)

    guard.record_failure(first_key, "first failed", now=now)

    first = guard.should_skip_start(first_key, now=now)
    second = guard.should_skip_start(second_key, now=now)
    assert first.skip is True
    assert first.reason == "first failed"
    assert second.skip is False
    assert second.consecutive_failures == 0


def test_unique_legacy_name_state_migrates_once_without_recounting(tmp_path):
    guard_path = tmp_path / "guard.json"
    legacy_entry = {
        "consecutive_failures": 2,
        "paused_until": None,
        "last_failure_reason": "legacy failure",
    }
    guard_path.write_text(
        json.dumps({"version": 1, "tasks": {"same-name": legacy_entry}}),
        encoding="utf-8",
    )
    guard = FailureGuard(path=str(guard_path))
    tasks = [{"id": 21, "task_name": "same-name"}]

    first_result = guard.migrate_legacy_task_keys(tasks)
    second_result = guard.migrate_legacy_task_keys(tasks)
    stored = json.loads(guard_path.read_text(encoding="utf-8"))

    assert first_result == {"migrated": 1, "ambiguous": 0}
    assert second_result == {"migrated": 0, "ambiguous": 0}
    assert "same-name" not in stored["tasks"]
    assert stored["tasks"][canonical_task_key(21)] == legacy_entry


def test_version_one_name_that_looks_canonical_still_migrates(tmp_path):
    guard_path = tmp_path / "guard.json"
    guard_path.write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": {
                    "task-id:42": {
                        "consecutive_failures": 1,
                        "last_failure_reason": "legacy display name",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    guard = FailureGuard(path=str(guard_path))

    result = guard.migrate_legacy_task_keys(
        [{"id": 73, "task_name": "task-id:42"}]
    )
    stored = json.loads(guard_path.read_text(encoding="utf-8"))

    assert result == {"migrated": 1, "ambiguous": 0}
    assert "task-id:42" not in stored["tasks"]
    assert stored["tasks"][canonical_task_key(73)]["consecutive_failures"] == 1


def test_ambiguous_legacy_name_state_is_retained_but_not_applied(tmp_path):
    guard_path = tmp_path / "guard.json"
    guard_path.write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": {
                    "duplicate": {
                        "consecutive_failures": 5,
                        "paused_until": "2099-01-01T00:00:00",
                        "last_failure_reason": "ambiguous failure",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    guard = FailureGuard(path=str(guard_path))

    result = guard.migrate_legacy_task_keys(
        [
            {"id": 31, "task_name": "duplicate"},
            {"id": 32, "task_name": "duplicate"},
        ]
    )

    stored = json.loads(guard_path.read_text(encoding="utf-8"))
    assert result == {"migrated": 0, "ambiguous": 1}
    assert "duplicate" in stored["tasks"]
    assert guard.should_skip_start(canonical_task_key(31)).skip is False
    assert guard.should_skip_start(canonical_task_key(32)).skip is False
