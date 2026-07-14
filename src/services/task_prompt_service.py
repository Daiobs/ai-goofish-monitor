"""Task-scoped criteria storage with atomic filesystem operations."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from src.failure_guard import canonical_task_key


class TaskPromptStore:
    def __init__(self, root_dir: str | Path = "prompts"):
        self.root_dir = Path(root_dir)

    def criteria_path(self, task_id: int) -> Path:
        stable_id = canonical_task_key(task_id).removeprefix("task-id:")
        return self.root_dir / "tasks" / stable_id / "criteria.txt"

    def criteria_path_string(self, task_id: int) -> str:
        return self.criteria_path(task_id).as_posix()

    def read_criteria(self, task_id: int) -> str | None:
        path = self.criteria_path(task_id)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def copy_legacy_criteria(self, task_id: int, source_value: str) -> bool:
        """Copy a compatible source under prompts/ into an ID-scoped target."""
        if not isinstance(source_value, str) or not source_value.strip():
            return False
        source = Path(source_value.strip())
        try:
            resolved_source = source.resolve(strict=True)
            resolved_root = self.root_dir.resolve(strict=True)
        except OSError:
            return False
        if (
            source.is_symlink()
            or not resolved_source.is_file()
            or not resolved_source.is_relative_to(resolved_root)
        ):
            return False
        try:
            content = resolved_source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        if not content.strip():
            return False
        self.write_criteria(task_id, content)
        return True

    def has_safe_criteria(self, task_id: int) -> bool:
        target = self.criteria_path(task_id)
        try:
            resolved_target = target.resolve(strict=True)
            resolved_root = self.root_dir.resolve(strict=True)
        except OSError:
            return False
        if (
            target.is_symlink()
            or not resolved_target.is_file()
            or not resolved_target.is_relative_to(resolved_root)
        ):
            return False
        try:
            return bool(resolved_target.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError):
            return False

    def write_criteria(self, task_id: int, content: str) -> Path:
        if not content or not content.strip():
            raise RuntimeError("AI 未能生成分析标准，返回内容为空。")

        target = self.criteria_path(task_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            fd, raw_temp_path = tempfile.mkstemp(
                prefix=".criteria.",
                suffix=".tmp",
                dir=target.parent,
            )
            temp_path = Path(raw_temp_path)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            temp_path = None
            return target
        except BaseException:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            self._remove_empty_task_directory(task_id)
            raise

    def restore_criteria(self, task_id: int, previous_content: str | None) -> None:
        if previous_content is None:
            self.criteria_path(task_id).unlink(missing_ok=True)
            self._remove_empty_task_directory(task_id)
            return
        self.write_criteria(task_id, previous_content)

    def delete_task_prompt(self, task_id: int) -> None:
        task_dir = self.criteria_path(task_id).parent
        if task_dir.exists():
            shutil.rmtree(task_dir)
        tasks_dir = self.root_dir / "tasks"
        try:
            tasks_dir.rmdir()
        except OSError:
            pass

    def _remove_empty_task_directory(self, task_id: int) -> None:
        task_dir = self.criteria_path(task_id).parent
        try:
            task_dir.rmdir()
        except OSError:
            return
        try:
            (self.root_dir / "tasks").rmdir()
        except OSError:
            pass
