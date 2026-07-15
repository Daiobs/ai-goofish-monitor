import subprocess
from pathlib import Path

import pytest

from scripts.ci.check_git_range_safety import (
    GitRangeSafetyError,
    scan_commit_range,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "CI Fixture")
    _git(repo, "config", "user.email", "ci-fixture@example.invalid")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _base_commit(repo: Path) -> str:
    (repo / "README.md").write_text("clean fixture\n", encoding="utf-8")
    return _commit(repo, "base")


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_secret_added_then_removed_is_still_rejected(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    candidate_value = "ghp_" + ("A1b2C3d4" * 5)
    candidate = repo / "temporary.txt"
    candidate.write_text(f"token={candidate_value}\n", encoding="utf-8")
    _commit(repo, "add secret")
    candidate.unlink()
    head = _commit(repo, "remove secret")

    issues = scan_commit_range(repo, base, head)

    assert "range-secret" in _codes(issues)
    assert candidate_value not in "\n".join(issue.render() for issue in issues)


def test_sqlite_added_then_removed_is_still_rejected(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    database = repo / "temporary.sqlite3"
    database.write_bytes(b"SQLite format 3\x00fictional")
    _commit(repo, "add database")
    database.unlink()
    head = _commit(repo, "remove database")

    assert "database-file" in _codes(scan_commit_range(repo, base, head))


def test_cookie_added_then_removed_is_still_rejected(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    cookie = repo / "browser-cookies.json"
    cookie.write_text('{"cookie": "fictional"}\n', encoding="utf-8")
    _commit(repo, "add cookie")
    cookie.unlink()
    head = _commit(repo, "remove cookie")

    assert "cookie-file" in _codes(scan_commit_range(repo, base, head))


def test_private_key_content_in_intermediate_commit_is_rejected(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    key = repo / "temporary.txt"
    key.write_text(
        "-----BEGIN "
        + "PRIVATE KEY-----\nfictional-material\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    _commit(repo, "add private key")
    key.unlink()
    head = _commit(repo, "remove private key")

    assert "range-secret" in _codes(scan_commit_range(repo, base, head))


def test_oversized_binary_in_intermediate_commit_is_rejected(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    binary = repo / "temporary.bin"
    binary.write_bytes(b"\x00" + (b"x" * 128))
    _commit(repo, "add binary")
    binary.unlink()
    head = _commit(repo, "remove binary")

    issues = scan_commit_range(repo, base, head, max_bytes=64)

    assert {"binary-artifact", "large-file"} <= _codes(issues)


def test_clean_commit_range_passes(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    (repo / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
    head = _commit(repo, "clean change")

    assert scan_commit_range(repo, base, head) == []


def test_unchanged_base_blob_is_not_rescanned(tmp_path):
    repo = _init_repo(tmp_path)
    existing = "ghp_" + ("B2c3D4e5" * 5)
    (repo / "historical.txt").write_text(
        f"historical={existing}\n",
        encoding="utf-8",
    )
    base = _commit(repo, "base with reviewed history")
    (repo / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
    head = _commit(repo, "clean change")

    assert scan_commit_range(repo, base, head) == []


def test_modified_blob_does_not_reject_unchanged_secret_candidate(tmp_path):
    repo = _init_repo(tmp_path)
    existing = "ghp_" + ("C3d4E5f6" * 5)
    source = repo / "historical.txt"
    source.write_text(f"historical={existing}\nVALUE=1\n", encoding="utf-8")
    base = _commit(repo, "base with reviewed finding")
    source.write_text(f"historical={existing}\nVALUE=2\n", encoding="utf-8")
    head = _commit(repo, "safe modification")

    assert scan_commit_range(repo, base, head) == []


def test_rename_to_forbidden_path_is_rejected(tmp_path):
    repo = _init_repo(tmp_path)
    source = repo / "settings.example"
    source.write_text("TOKEN=replace-me\n", encoding="utf-8")
    base = _commit(repo, "base")
    _git(repo, "mv", "settings.example", ".env")
    head = _commit(repo, "unsafe rename")

    assert "sensitive-file" in _codes(scan_commit_range(repo, base, head))


def test_invalid_base_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    head = _base_commit(repo)

    with pytest.raises(GitRangeSafetyError, match="git command failed"):
        scan_commit_range(repo, "0" * 40, head)


def test_empty_range_passes(tmp_path):
    repo = _init_repo(tmp_path)
    head = _base_commit(repo)

    assert scan_commit_range(repo, head, head) == []


def test_baseline_and_allowlist_changes_cannot_mask_range_secret(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    candidate_value = "ghp_" + ("Z9y8X7w6" * 5)
    (repo / ".secrets.baseline").write_text(
        '{"results": {"temporary.txt": []}}\n',
        encoding="utf-8",
    )
    allowlist = repo / "ci" / "public-secret-allowlist.yml"
    allowlist.parent.mkdir()
    allowlist.write_text(
        "version: 1\nentries:\n  - path: temporary.txt\n"
        "    pattern: '.*'\n",
        encoding="utf-8",
    )
    (repo / "temporary.txt").write_text(
        f"token={candidate_value}\n",
        encoding="utf-8",
    )
    head = _commit(repo, "attempt to mask secret")

    issues = scan_commit_range(repo, base, head)

    assert "range-secret" in _codes(issues)
    assert candidate_value not in "\n".join(issue.render() for issue in issues)
