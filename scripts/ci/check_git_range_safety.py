#!/usr/bin/env python3
"""Inspect every introduced Git blob in an event commit range."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.check_detect_secrets import (  # noqa: E402
    SecretScanError,
    run_detect_secrets,
    snapshot_findings,
)
from scripts.ci.check_public_repo_safety import (  # noqa: E402
    MAX_TRACKED_FILE_BYTES,
    SafetyIssue,
    inspect_blob,
)


EMPTY_TREE_SHA = "".join(
    ("4b825dc6", "42cb6eb9", "a060e54b", "f8d69288", "fbee4904")
)


class GitRangeSafetyError(RuntimeError):
    """Raised when a requested Git range cannot be inspected completely."""


@dataclass(frozen=True)
class BlobChange:
    commit: str
    status: str
    old_path: str | None
    new_path: str
    old_blob: str | None
    new_blob: str


def _git(repo_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise GitRangeSafetyError(
            f"git command failed ({args[0] if args else 'unknown'})"
        )
    return result.stdout


def _resolve_commit(repo_root: Path, revision: str) -> str:
    if not revision:
        raise GitRangeSafetyError("commit revision is empty")
    resolved = _git(repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    commit = resolved.decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise GitRangeSafetyError("git returned an invalid commit object")
    return commit


def commits_in_range(repo_root: Path, base_revision: str, head_revision: str) -> tuple[str, ...]:
    base = _resolve_commit(repo_root, base_revision)
    head = _resolve_commit(repo_root, head_revision)
    if base == head:
        return ()
    output = _git(repo_root, "rev-list", "--reverse", f"{base}..{head}")
    commits = tuple(line for line in output.decode("ascii").splitlines() if line)
    if any(not re.fullmatch(r"[0-9a-f]{40,64}", commit) for commit in commits):
        raise GitRangeSafetyError("git returned an invalid range commit")
    return commits


def _commit_parents(repo_root: Path, commit: str) -> tuple[str, ...]:
    fields = _git(repo_root, "rev-list", "--parents", "-n", "1", commit).decode(
        "ascii"
    ).split()
    if not fields or fields[0] != commit:
        raise GitRangeSafetyError("unable to resolve commit parents")
    return tuple(fields[1:]) or (EMPTY_TREE_SHA,)


def _tree_blob(repo_root: Path, treeish: str, path: str) -> str:
    output = _git(repo_root, "ls-tree", "-z", treeish, "--", path)
    entries = [entry for entry in output.split(b"\0") if entry]
    if len(entries) != 1 or b"\t" not in entries[0]:
        raise GitRangeSafetyError("changed path does not resolve to one Git object")
    metadata, listed_path = entries[0].split(b"\t", 1)
    fields = metadata.split()
    if len(fields) != 3 or fields[1] != b"blob":
        raise GitRangeSafetyError("changed path is not a regular Git blob")
    try:
        decoded_path = listed_path.decode("utf-8")
        blob = fields[2].decode("ascii")
    except UnicodeDecodeError as exc:
        raise GitRangeSafetyError("changed Git path or object is invalid") from exc
    if decoded_path != path or not re.fullmatch(r"[0-9a-f]{40,64}", blob):
        raise GitRangeSafetyError("changed Git path or blob is inconsistent")
    return blob


def _decode_diff_path(value: bytes) -> str:
    try:
        path = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitRangeSafetyError("commit range contains a non-UTF-8 path") from exc
    if not path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
        raise GitRangeSafetyError("commit range contains an unsafe path")
    return path


def _parent_changes(repo_root: Path, parent: str, commit: str) -> tuple[BlobChange, ...]:
    output = _git(
        repo_root,
        "diff-tree",
        "-r",
        "--no-commit-id",
        "--name-status",
        "-z",
        "-M",
        "-C",
        "--diff-filter=AMRCT",
        parent,
        commit,
    )
    fields = [field for field in output.split(b"\0") if field]
    changes: list[BlobChange] = []
    index = 0
    while index < len(fields):
        try:
            status = fields[index].decode("ascii")
        except UnicodeDecodeError as exc:
            raise GitRangeSafetyError("commit range contains an invalid status") from exc
        index += 1
        kind = status[:1]
        if kind in {"R", "C"}:
            if index + 1 >= len(fields):
                raise GitRangeSafetyError("commit range rename record is incomplete")
            old_path = _decode_diff_path(fields[index])
            new_path = _decode_diff_path(fields[index + 1])
            index += 2
        elif kind in {"A", "M", "T"}:
            if index >= len(fields):
                raise GitRangeSafetyError("commit range path record is incomplete")
            new_path = _decode_diff_path(fields[index])
            old_path = None if kind == "A" else new_path
            index += 1
        else:
            raise GitRangeSafetyError("commit range contains an unsupported change")

        new_blob = _tree_blob(repo_root, commit, new_path)
        compare_old_path = old_path if kind != "C" else None
        old_blob = (
            _tree_blob(repo_root, parent, compare_old_path)
            if compare_old_path is not None
            else None
        )
        changes.append(
            BlobChange(
                commit=commit,
                status=kind,
                old_path=compare_old_path,
                new_path=new_path,
                old_blob=old_blob,
                new_blob=new_blob,
            )
        )
    return tuple(changes)


def changes_in_range(
    repo_root: Path,
    base_revision: str,
    head_revision: str,
) -> tuple[BlobChange, ...]:
    changes: list[BlobChange] = []
    for commit in commits_in_range(repo_root, base_revision, head_revision):
        for parent in _commit_parents(repo_root, commit):
            changes.extend(_parent_changes(repo_root, parent, commit))
    return tuple(changes)


def _blob_size(repo_root: Path, blob: str) -> int:
    output = _git(repo_root, "cat-file", "-s", blob)
    try:
        size = int(output.decode("ascii").strip())
    except ValueError as exc:
        raise GitRangeSafetyError("git returned an invalid blob size") from exc
    if size < 0:
        raise GitRangeSafetyError("git returned a negative blob size")
    return size


def _blob_content(repo_root: Path, blob: str) -> bytes:
    return _git(repo_root, "cat-file", "blob", blob)


def _safe_blob_filename(path: str) -> str:
    filename = PurePosixPath(path).name or "blob.txt"
    return re.sub(r"[^A-Za-z0-9._-]", "_", filename)


def _detect_secret_candidates(
    blobs: dict[str, tuple[str, bytes]],
) -> dict[str, frozenset[tuple[str, str]]]:
    if not blobs:
        return {}
    try:
        with tempfile.TemporaryDirectory(prefix="git-range-secrets-") as temp_dir:
            temp_root = Path(temp_dir)
            paths: list[str] = []
            path_to_blob: dict[str, str] = {}
            for blob, (original_path, content) in blobs.items():
                if original_path == ".secrets.baseline":
                    continue
                relative_path = (
                    Path("blobs") / blob / _safe_blob_filename(original_path)
                ).as_posix()
                output_path = temp_root / relative_path
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(content)
                paths.append(relative_path)
                path_to_blob[relative_path] = blob

            findings = (
                snapshot_findings(run_detect_secrets(temp_root, tuple(paths)))
                if paths
                else ()
            )
    except (OSError, SecretScanError) as exc:
        raise GitRangeSafetyError(
            f"commit-range secret scanner failed ({type(exc).__name__})"
        ) from exc

    candidates: dict[str, set[tuple[str, str]]] = {
        blob: set() for blob in blobs
    }
    for finding in findings:
        blob = path_to_blob.get(finding.path)
        if blob is None:
            raise GitRangeSafetyError("secret scanner returned an unknown blob path")
        candidates[blob].add((finding.secret_type, finding.hashed_secret))
    return {blob: frozenset(values) for blob, values in candidates.items()}


def scan_commit_range(
    repo_root: Path,
    base_revision: str,
    head_revision: str,
    *,
    max_bytes: int = MAX_TRACKED_FILE_BYTES,
) -> list[SafetyIssue]:
    repo_root = repo_root.resolve()
    changes = changes_in_range(repo_root, base_revision, head_revision)
    issues: list[SafetyIssue] = []
    scan_blobs: dict[str, tuple[str, bytes]] = {}
    content_cache: dict[str, bytes] = {}

    for change in changes:
        size = _blob_size(repo_root, change.new_blob)
        if size > max_bytes:
            policy_content = b"x" * (max_bytes + 1)
        else:
            policy_content = content_cache.setdefault(
                change.new_blob,
                _blob_content(repo_root, change.new_blob),
            )
            if len(policy_content) != size:
                raise GitRangeSafetyError("git blob size changed during inspection")
            scan_blobs.setdefault(
                change.new_blob,
                (change.new_path, policy_content),
            )
        issues.extend(
            SafetyIssue(
                issue.code,
                issue.path,
                f"{issue.detail} in commit {change.commit[:12]}",
            )
            for issue in inspect_blob(
                change.new_path,
                policy_content,
                max_bytes=max_bytes,
                scan_secrets=False,
            )
        )

        if change.old_blob is not None and change.old_blob not in scan_blobs:
            old_size = _blob_size(repo_root, change.old_blob)
            if old_size <= max_bytes:
                old_content = content_cache.setdefault(
                    change.old_blob,
                    _blob_content(repo_root, change.old_blob),
                )
                if len(old_content) != old_size:
                    raise GitRangeSafetyError("parent Git blob size changed during inspection")
                scan_blobs[change.old_blob] = (
                    change.old_path or change.new_path,
                    old_content,
                )

    candidates = _detect_secret_candidates(scan_blobs)
    for change in changes:
        new_candidates = candidates.get(change.new_blob, frozenset())
        old_candidates = candidates.get(change.old_blob or "", frozenset())
        for secret_type, _hashed_secret in sorted(new_candidates - old_candidates):
            issues.append(
                SafetyIssue(
                    "range-secret",
                    change.new_path,
                    f"new {secret_type} candidate in commit {change.commit[:12]}",
                )
            )

    unique = {
        (issue.code, issue.path, issue.detail): issue
        for issue in issues
    }
    return sorted(unique.values(), key=lambda issue: (issue.path, issue.code, issue.detail))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base-sha", default=os.environ.get("BASE_SHA", ""))
    parser.add_argument("--head-sha", default=os.environ.get("HEAD_SHA", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.base_sha and not args.head_sha:
        print("commit-range safety skipped: workflow_dispatch has no event range")
        return 0
    if not args.base_sha or not args.head_sha:
        print("commit-range safety failed: base and head must both be set", file=sys.stderr)
        return 1
    try:
        issues = scan_commit_range(
            args.repo_root,
            args.base_sha,
            args.head_sha,
        )
    except (OSError, GitRangeSafetyError) as exc:
        print(f"commit-range safety failed: {exc}", file=sys.stderr)
        return 1
    if issues:
        for issue in issues:
            print(issue.render(), file=sys.stderr)
        print(
            f"commit-range safety failed with {len(issues)} issue(s)",
            file=sys.stderr,
        )
        return 1
    print("commit-range safety passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
