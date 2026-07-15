#!/usr/bin/env python3
"""Reject tracked files and workflows that are unsafe for a public repository."""

from __future__ import annotations

import argparse
import copy
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

import yaml


MAX_TRACKED_FILE_BYTES = 1024 * 1024
IMAGE_EXTENSIONS = {".avif", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
ARCHIVE_EXTENSIONS = {".7z", ".bak", ".bz2", ".gz", ".rar", ".tar", ".tgz", ".xz", ".zip"}
DATABASE_EXTENSIONS = {".db", ".db3", ".sqlite", ".sqlite3"}
PRIVATE_KEY_EXTENSIONS = {".key", ".p12", ".pfx"}
BINARY_ARTIFACT_EXTENSIONS = {
    ".avi",
    ".bin",
    ".mkv",
    ".mov",
    ".mp4",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
    ".webm",
}
TRUSTED_ACTION_OWNERS = {"actions", "anthropics", "docker", "oven-sh"}
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
ASSOCIATION_FIELDS = {
    "discussion": "github.event.discussion.author_association",
    "discussion_comment": "github.event.comment.author_association",
    "issue_comment": "github.event.comment.author_association",
    "issues": "github.event.issue.author_association",
    "pull_request": "github.event.pull_request.author_association",
    "pull_request_review": "github.event.review.author_association",
    "pull_request_review_comment": "github.event.comment.author_association",
}
SENSITIVE_WRITE_PERMISSIONS = frozenset(
    {"actions", "contents", "id-token", "issues", "packages", "pull-requests"}
)
RUNTIME_DIRECTORIES = {
    "browser-profile",
    "cookies",
    "data",
    "images",
    "logs",
    "state",
    "user-data-dir",
}
SECRET_PATTERNS = (
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("api-token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
)


class SafetyConfigError(RuntimeError):
    """Raised for malformed versioned safety configuration."""


@dataclass(frozen=True)
class SafetyIssue:
    code: str
    path: str
    detail: str

    def render(self) -> str:
        return f"{self.code}: {self.path}: {self.detail}"


@dataclass(frozen=True)
class SecretAllowRule:
    path: str
    pattern_text: str
    pattern: re.Pattern[str]


class WorkflowLoader(yaml.SafeLoader):
    """YAML loader that keeps the workflow key `on` as a string."""


WorkflowLoader.yaml_implicit_resolvers = copy.deepcopy(
    yaml.SafeLoader.yaml_implicit_resolvers
)
for resolver_key, resolvers in tuple(WorkflowLoader.yaml_implicit_resolvers.items()):
    WorkflowLoader.yaml_implicit_resolvers[resolver_key] = [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:bool"
    ]


def _validate_exact_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or any(char in value for char in "*?[]{}")
    ):
        raise SafetyConfigError(f"{label} must use an exact repository-relative path")
    return path.as_posix()


def load_file_allowlist(path: Path) -> frozenset[str]:
    entries: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entry = _validate_exact_path(line.split("\t", 1)[0], "file allowlist entry")
        entries.append(entry)
    duplicates = sorted({entry for entry in entries if entries.count(entry) > 1})
    if duplicates:
        raise SafetyConfigError("duplicate file allowlist entries")
    return frozenset(entries)


def load_secret_allowlist(path: Path) -> tuple[SecretAllowRule, ...]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SafetyConfigError(
            f"unable to load secret allowlist ({type(exc).__name__})"
        ) from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SafetyConfigError("secret allowlist must declare version 1")
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        raise SafetyConfigError("secret allowlist entries must be a list")

    rules: list[SecretAllowRule] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SafetyConfigError("secret allowlist entries must be mappings")
        exact_path = _validate_exact_path(str(entry.get("path", "")), "secret allowlist path")
        pattern_text = str(entry.get("pattern", ""))
        if len(pattern_text) < 12 or ".*" in pattern_text:
            raise SafetyConfigError("secret allowlist patterns must be narrow and explicit")
        try:
            pattern = re.compile(pattern_text)
        except re.error as exc:
            raise SafetyConfigError("invalid secret allowlist regex") from exc
        key = (exact_path, pattern_text)
        if key in seen:
            raise SafetyConfigError("duplicate secret allowlist entry")
        seen.add(key)
        rules.append(SecretAllowRule(exact_path, pattern_text, pattern))
    return tuple(rules)


def _secret_is_allowed(path: str, value: str, rules: Iterable[SecretAllowRule]) -> bool:
    return any(
        rule.path == path and rule.pattern.fullmatch(value)
        for rule in rules
    )


def scan_text_secrets(
    path: str,
    text: str,
    rules: Iterable[SecretAllowRule],
) -> list[SafetyIssue]:
    issues: list[SafetyIssue] = []
    for secret_type, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if _secret_is_allowed(path, match.group(0), rules):
                continue
            line_number = text.count("\n", 0, match.start()) + 1
            issues.append(
                SafetyIssue(
                    "secret-pattern",
                    path,
                    f"{secret_type} candidate on line {line_number}",
                )
            )
    return issues


def _path_policy_issues(path: str) -> list[SafetyIssue]:
    relative = PurePosixPath(path)
    lower_path = path.lower()
    name = relative.name.lower()
    suffix = relative.suffix.lower()
    parts = {part.lower() for part in relative.parts}
    issues: list[SafetyIssue] = []

    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        issues.append(SafetyIssue("sensitive-file", path, "environment file is not public"))
    if suffix in DATABASE_EXTENSIONS or name.endswith((".sqlite-wal", ".sqlite-shm", ".db-wal", ".db-shm")):
        issues.append(SafetyIssue("database-file", path, "database files are not public artifacts"))
    if suffix == ".jsonl":
        issues.append(SafetyIssue("runtime-result", path, "JSONL runtime results are forbidden"))
    if suffix == ".log" or ("logs" in parts and name != ".gitkeep"):
        issues.append(SafetyIssue("runtime-log", path, "runtime logs are forbidden"))
    if name == "docker-compose.8008.yaml":
        issues.append(SafetyIssue("local-config", path, "local compose override is forbidden"))
    if re.search(r"(?:^|[-_.])(cookie|cookies)(?:[-_.]|$)", name) and suffix in {"", ".json", ".txt"}:
        issues.append(SafetyIssue("cookie-file", path, "cookie material is forbidden"))
    if "cookies" in parts and name != ".gitkeep":
        issues.append(SafetyIssue("cookie-file", path, "cookie directory content is forbidden"))
    if name in {"auth-state.json", "state.json", "storage-state.json", "storage_state.json"}:
        issues.append(SafetyIssue("browser-state", path, "browser or authentication state is forbidden"))
    if "playwright" in parts or "browser-profile" in parts or "user-data-dir" in parts:
        issues.append(SafetyIssue("browser-profile", path, "browser profiles are forbidden"))
    if name.startswith("proxy") and suffix in {".env", ".json", ".toml", ".yaml", ".yml"}:
        issues.append(SafetyIssue("proxy-config", path, "local proxy configuration is forbidden"))
    if suffix in PRIVATE_KEY_EXTENSIONS or name.endswith("private.pem"):
        issues.append(SafetyIssue("private-key-file", path, "private key containers are forbidden"))
    if suffix in ARCHIVE_EXTENSIONS or name.endswith((".backup", ".old")):
        issues.append(SafetyIssue("archive-file", path, "archives and backups are forbidden"))
    if name == "core" or name.startswith("core."):
        issues.append(SafetyIssue("core-dump", path, "core dumps are forbidden"))
    if suffix in IMAGE_EXTENSIONS:
        issues.append(SafetyIssue("binary-asset", path, "image assets require an exact exception"))
    if suffix in BINARY_ARTIFACT_EXTENSIONS:
        issues.append(SafetyIssue("binary-artifact", path, "models and media binaries are forbidden"))
    if relative.parts[0].lower() in RUNTIME_DIRECTORIES and name != ".gitkeep":
        issues.append(SafetyIssue("runtime-directory", path, "runtime directory content is forbidden"))
    if "price_history" in lower_path and suffix in {".csv", ".json", ".jsonl"}:
        issues.append(SafetyIssue("price-history", path, "price history runtime data is forbidden"))
    return issues


def inspect_tracked_file(
    repo_root: Path,
    path: str,
    file_allowlist: frozenset[str],
    secret_rules: Iterable[SecretAllowRule],
    max_bytes: int = MAX_TRACKED_FILE_BYTES,
) -> list[SafetyIssue]:
    file_path = repo_root / path
    if not file_path.is_file():
        return [SafetyIssue("missing-tracked-file", path, "tracked path is not a regular file")]

    return inspect_blob(
        path,
        file_path.read_bytes(),
        file_allowlisted=path in file_allowlist,
        secret_rules=secret_rules,
        max_bytes=max_bytes,
    )


def inspect_blob(
    path: str,
    content: bytes,
    *,
    file_allowlisted: bool = False,
    secret_rules: Iterable[SecretAllowRule] = (),
    max_bytes: int = MAX_TRACKED_FILE_BYTES,
    scan_secrets: bool = True,
) -> list[SafetyIssue]:
    """Apply public file policy to an in-memory Git blob without exposing content."""

    issues = _path_policy_issues(path)
    if file_allowlisted:
        issues = [issue for issue in issues if issue.code not in {"binary-asset"}]

    if len(content) > max_bytes and not file_allowlisted:
        issues.append(
            SafetyIssue("large-file", path, f"tracked file exceeds {max_bytes} bytes")
        )
        return issues

    if b"\x00" in content[:8192] and not file_allowlisted:
        issues.append(SafetyIssue("binary-file", path, "binary content requires an exact exception"))
        return issues

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        if not file_allowlisted:
            issues.append(SafetyIssue("binary-file", path, "non-UTF-8 content requires an exact exception"))
        return issues
    if scan_secrets:
        issues.extend(scan_text_secrets(path, text, secret_rules))
    return issues


def _workflow_trigger_names(triggers: object) -> frozenset[str]:
    if isinstance(triggers, str):
        return frozenset({triggers})
    if isinstance(triggers, list):
        return frozenset(str(trigger) for trigger in triggers)
    if isinstance(triggers, dict):
        return frozenset(str(trigger) for trigger in triggers)
    return frozenset()


def _contains_repository_secret(value: object) -> bool:
    return bool(re.search(r"\$\{\{\s*secrets(?:\.|\[)", str(value), re.IGNORECASE))


def _job_receives_repository_secret(job: dict, workflow_env: object) -> bool:
    return (
        _contains_repository_secret(workflow_env)
        or _contains_repository_secret(job)
        or str(job.get("secrets", "")).lower() == "inherit"
    )


def _split_top_level(expression: str, operator: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and expression.startswith(operator, index):
            parts.append(expression[start:index].strip())
            index += len(operator)
            start = index
            continue
        index += 1
    parts.append(expression[start:].strip())
    return parts


def _expression_syntax_is_valid(expression: str) -> bool:
    depth = 0
    quote = ""
    escaped = False
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
        elif char in {"&", "|"}:
            if index + 1 >= len(expression) or expression[index + 1] != char:
                return False
            index += 1
        index += 1
    return depth == 0 and not quote and not escaped


def _outer_parentheses_wrap(expression: str) -> bool:
    if not expression.startswith("(") or not expression.endswith(")"):
        return False
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(expression):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(expression) - 1:
                return False
    return depth == 0 and not quote


def _strip_expression_wrapper(expression: str) -> str:
    expression = expression.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    while _outer_parentheses_wrap(expression):
        expression = expression[1:-1].strip()
    return expression


def trusted_association_gate_issues(
    condition: object,
    event_names: Iterable[str],
) -> list[str]:
    if not isinstance(condition, str) or not condition.strip():
        return ["missing trusted author association gate"]

    expression = condition.strip()
    has_open_wrapper = expression.startswith("${{")
    has_close_wrapper = expression.endswith("}}")
    if has_open_wrapper != has_close_wrapper:
        return ["trusted event gate expression is malformed"]
    if has_open_wrapper:
        expression = expression[3:-2].strip()
        if "${{" in expression or "}}" in expression:
            return ["trusted event gate expression is malformed"]
    if not expression or not _expression_syntax_is_valid(expression):
        return ["trusted event gate expression is malformed"]

    expected_events = {str(event_name) for event_name in event_names}
    if not expected_events:
        return ["sensitive job has no declared trigger events"]

    branches = _split_top_level(_strip_expression_wrapper(expression), "||")
    problems: list[str] = []
    seen_events: set[str] = set()
    event_comparison = re.compile(
        r"github\.event_name\s*==\s*(['\"])([A-Za-z0-9_]+)\1"
    )
    exact_event_conjunct = re.compile(
        r"^github\.event_name==(['\"])([A-Za-z0-9_]+)\1$"
    )
    trusted_values = '["' + '","'.join(TRUSTED_ASSOCIATIONS) + '"]'

    for branch_number, raw_branch in enumerate(branches, start=1):
        branch = _strip_expression_wrapper(raw_branch)
        if not branch:
            problems.append(f"OR branch {branch_number} is empty")
            continue
        conjuncts = [
            _strip_expression_wrapper(part)
            for part in _split_top_level(branch, "&&")
        ]
        if any(not part for part in conjuncts):
            problems.append(f"OR branch {branch_number} is malformed")
            continue
        compact_branch = re.sub(r"\s+", "", branch)
        event_occurrences = list(event_comparison.finditer(branch))
        event_conjuncts = [
            match
            for part in conjuncts
            if (match := exact_event_conjunct.fullmatch(re.sub(r"\s+", "", part)))
        ]
        if (
            len(event_occurrences) != 1
            or compact_branch.count("github.event_name") != 1
            or len(event_conjuncts) != 1
        ):
            problems.append(
                f"OR branch {branch_number} must contain exactly one top-level event discriminator"
            )
            continue

        event_name = event_conjuncts[0].group(2)
        if event_name not in expected_events:
            problems.append(f"OR branch {branch_number} references an unknown trigger event")
            continue
        if event_name in seen_events:
            problems.append(f"{event_name} has more than one executable OR branch")
            continue
        seen_events.add(event_name)

        expected_field = ASSOCIATION_FIELDS.get(event_name)
        if expected_field is None:
            continue
        expected_gate = (
            f"contains(fromJSON('{trusted_values}'),"
            f"{expected_field})"
        )
        compact_conjuncts = [re.sub(r"\s+", "", part) for part in conjuncts]
        if (
            compact_conjuncts.count(expected_gate) != 1
            or compact_branch.count("author_association") != 1
        ):
            problems.append(
                f"{event_name} must fail closed to OWNER, MEMBER, or COLLABORATOR"
            )

    for event_name in sorted(expected_events - seen_events):
        problems.append(f"{event_name} must have exactly one executable OR branch")
    return problems


def _job_has_sensitive_write(job: dict) -> bool:
    permissions = job.get("permissions", {})
    if isinstance(permissions, str):
        return permissions.lower() == "write-all"
    if not isinstance(permissions, dict):
        return False
    return any(
        str(name).lower() in SENSITIVE_WRITE_PERMISSIONS
        and str(value).lower() == "write"
        for name, value in permissions.items()
    )


def _job_run_steps(job: dict) -> tuple[dict, ...]:
    steps = job.get("steps", [])
    if not isinstance(steps, list):
        return ()
    return tuple(
        step
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    )


def _has_frozen_local_binary_install(job: dict, repo_root: Path) -> bool:
    default_working_directory = ""
    defaults = job.get("defaults", {})
    if isinstance(defaults, dict) and isinstance(defaults.get("run"), dict):
        default_working_directory = str(defaults["run"].get("working-directory", ""))

    for step in _job_run_steps(job):
        if not re.search(r"(?m)(?:^|\s)npm\s+ci(?:\s|$)", step["run"]):
            continue
        working_directory = str(
            step.get("working-directory", default_working_directory)
        )
        lock_path = repo_root / working_directory / "package-lock.json"
        if lock_path.is_file():
            return True
    return False


def _dynamic_package_execution_issues(
    job: dict,
    repo_root: Path,
    relative_path: str,
) -> list[SafetyIssue]:
    issues: list[SafetyIssue] = []
    uses_local_binary = False
    for step in _job_run_steps(job):
        run = step["run"]
        if re.search(r"(?m)(?:^|\s)(?:bunx|npx|pnpx)(?:\s|$)", run):
            issues.append(
                SafetyIssue(
                    "workflow-dynamic-package-exec",
                    relative_path,
                    "secret-bearing jobs may not use dynamic package executors",
                )
            )
        for line in run.splitlines():
            if re.search(r"(?:^|\s)npm\s+exec(?:\s|$)", line) and not re.search(
                r"(?:^|\s)--(?:offline|no-install)(?:\s|$)", line
            ):
                issues.append(
                    SafetyIssue(
                        "workflow-dynamic-package-exec",
                        relative_path,
                        "npm exec in a secret-bearing job must prohibit downloads",
                    )
                )
        if re.search(r"(?:^|\s)(?:\./)?node_modules/\.bin/", run):
            uses_local_binary = True
    if uses_local_binary and not _has_frozen_local_binary_install(job, repo_root):
        issues.append(
            SafetyIssue(
                "workflow-local-binary-lock",
                relative_path,
                "local package binaries require npm ci and a committed package-lock.json",
            )
        )
    return issues


def _load_workflow(path: Path) -> dict | None:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=WorkflowLoader)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def inspect_workflow(path: Path, relative_path: str) -> list[SafetyIssue]:
    text = path.read_text(encoding="utf-8")
    data = _load_workflow(path)
    if data is None:
        return [SafetyIssue("workflow-yaml", relative_path, "workflow YAML is invalid")]

    issues: list[SafetyIssue] = []
    triggers = data.get("on", {})
    if isinstance(triggers, dict) and "pull_request_target" in triggers:
        issues.append(SafetyIssue("workflow-trigger", relative_path, "pull_request_target is forbidden"))
    if re.search(r"(?m)^\s*pull_request_target\s*:", text):
        if not any(issue.code == "workflow-trigger" for issue in issues):
            issues.append(SafetyIssue("workflow-trigger", relative_path, "pull_request_target is forbidden"))

    permissions = data.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("contents") != "read":
        issues.append(SafetyIssue("workflow-permissions", relative_path, "default permissions must set contents: read"))
    elif any(str(value).lower() == "write" for value in permissions.values()):
        issues.append(SafetyIssue("workflow-default-write", relative_path, "default write permissions are forbidden"))

    uses_pattern = re.compile(
        r"^\s*(?:-\s*)?uses:\s*([^\s#]+)(?:\s+#\s*(.+))?$"
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = uses_pattern.match(line)
        if not match:
            continue
        target, version_comment = match.groups()
        if target.startswith("./"):
            continue
        if target.startswith("docker://"):
            pinned = bool(re.fullmatch(r"docker://[^@\s]+@sha256:[0-9a-f]{64}", target))
        else:
            pinned = bool(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", target))
            owner = target.split("/", 1)[0]
            if owner not in TRUSTED_ACTION_OWNERS:
                issues.append(SafetyIssue("workflow-action-owner", relative_path, f"unapproved action owner on line {line_number}"))
        if not pinned:
            issues.append(SafetyIssue("workflow-action-pin", relative_path, f"unfixed action on line {line_number}"))
        elif not version_comment:
            issues.append(SafetyIssue("workflow-action-comment", relative_path, f"pinned action lacks version comment on line {line_number}"))

    trigger_names = _workflow_trigger_names(triggers)
    pull_request_workflow = "pull_request" in trigger_names
    workflow_env = data.get("env", {})
    jobs = data.get("jobs", {})
    if isinstance(jobs, dict):
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            secret_job = _job_receives_repository_secret(job, workflow_env)
            write_job = _job_has_sensitive_write(job)
            if pull_request_workflow and secret_job:
                issues.append(
                    SafetyIssue(
                        "workflow-pr-secret",
                        relative_path,
                        "pull_request jobs may not receive repository secrets",
                    )
                )
            if trigger_names and (secret_job or write_job):
                gate_problems = trusted_association_gate_issues(
                    job.get("if"),
                    trigger_names,
                )
                for problem in gate_problems:
                    issues.append(
                        SafetyIssue(
                            "workflow-untrusted-trigger-gate",
                            relative_path,
                            problem,
                        )
                    )
            if secret_job:
                issues.extend(
                    _dynamic_package_execution_issues(
                        job,
                        path.resolve().parents[2],
                        relative_path,
                    )
                )
            for step in _job_run_steps(job):
                run = step["run"]
                if _contains_repository_secret(run):
                    issues.append(SafetyIssue("workflow-secret-shell", relative_path, "shell commands may not interpolate repository secrets"))

    if re.search(r"(?im)\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z|k)?sh\b", text):
        issues.append(SafetyIssue("workflow-remote-shell", relative_path, "unverified remote shell execution is forbidden"))
    if relative_path.endswith("public-repository-safety.yml") and "${{ secrets." in text:
        issues.append(SafetyIssue("safety-workflow-secret", relative_path, "public safety workflow must not use secrets"))
    return issues


def git_tracked_files(repo_root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SafetyConfigError("unable to enumerate tracked files")
    return tuple(
        entry.decode("utf-8")
        for entry in result.stdout.split(b"\0")
        if entry
    )


def scan_repository(
    repo_root: Path,
    file_allowlist_path: Path,
    secret_allowlist_path: Path,
) -> list[SafetyIssue]:
    tracked = git_tracked_files(repo_root)
    tracked_set = set(tracked)
    file_allowlist = load_file_allowlist(file_allowlist_path)
    secret_rules = load_secret_allowlist(secret_allowlist_path)
    issues: list[SafetyIssue] = []

    for stale in sorted(file_allowlist - tracked_set):
        issues.append(SafetyIssue("stale-file-exception", stale, "allowlisted path is not tracked"))
    for rule in secret_rules:
        if rule.path not in tracked_set:
            issues.append(SafetyIssue("stale-secret-exception", rule.path, "secret exception path is not tracked"))

    for path in tracked:
        issues.extend(
            inspect_tracked_file(repo_root, path, file_allowlist, secret_rules)
        )
        if path.startswith(".github/workflows/") and Path(path).suffix in {".yml", ".yaml"}:
            issues.extend(inspect_workflow(repo_root / path, path))
    return sorted(issues, key=lambda issue: (issue.path, issue.code, issue.detail))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--file-allowlist",
        type=Path,
        default=Path("ci/public-file-allowlist.txt"),
    )
    parser.add_argument(
        "--secret-allowlist",
        type=Path,
        default=Path("ci/public-secret-allowlist.yml"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        issues = scan_repository(
            repo_root,
            repo_root / args.file_allowlist,
            repo_root / args.secret_allowlist,
        )
    except (OSError, SafetyConfigError) as exc:
        print(f"public repository safety configuration failed: {exc}", file=sys.stderr)
        return 1
    if issues:
        for issue in issues:
            print(issue.render(), file=sys.stderr)
        print(f"public repository safety failed with {len(issues)} issue(s)", file=sys.stderr)
        return 1
    print(f"public repository safety passed for {len(git_tracked_files(repo_root))} tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
