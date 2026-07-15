import subprocess
from pathlib import Path

import pytest

from scripts.ci.check_git_range_safety import (
    GitRangeSafetyError,
    scan_commit_range,
)
from scripts.ci.check_public_repo_safety import SafetyConfigError


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


def _write_file_allowlist(repo: Path, *paths: str) -> Path:
    allowlist = repo / "ci" / "public-file-allowlist.txt"
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text("".join(f"{path}\tfixture asset\n" for path in paths), encoding="utf-8")
    return allowlist


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


def test_head_retained_png_with_exact_file_allowlist_passes(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    image = repo / "static" / "fixture.png"
    image.parent.mkdir()
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00fixture")
    allowlist = _write_file_allowlist(repo, "static/fixture.png")
    head = _commit(repo, "add reviewed image")

    assert scan_commit_range(
        repo,
        base,
        head,
        file_allowlist_path=allowlist,
    ) == []


def test_head_retained_png_without_file_allowlist_fails(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    image = repo / "static" / "fixture.png"
    image.parent.mkdir()
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00fixture")
    head = _commit(repo, "add unreviewed image")

    assert {"binary-asset", "binary-file"} <= _codes(
        scan_commit_range(repo, base, head)
    )


def test_stale_file_allowlist_path_fails(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    allowlist = _write_file_allowlist(repo, "static/missing.png")
    head = _commit(repo, "add stale allowlist")

    issues = scan_commit_range(
        repo,
        base,
        head,
        file_allowlist_path=allowlist,
    )

    assert "stale-file-exception" in _codes(issues)


def test_head_file_allowlist_rejects_symlink_blob(tmp_path):
    repo = _init_repo(tmp_path)
    target = repo / "fixture.txt"
    target.write_text("fixture\n", encoding="utf-8")
    image = repo / "static" / "fixture.png"
    image.parent.mkdir()
    image.symlink_to("../fixture.txt")
    allowlist = _write_file_allowlist(repo, "static/fixture.png")
    head = _commit(repo, "add allowlisted symlink")

    issues = scan_commit_range(
        repo,
        head,
        head,
        file_allowlist_path=allowlist,
    )

    assert "stale-file-exception" in _codes(issues)


def test_intermediate_png_added_then_deleted_is_not_allowlisted(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    image = repo / "static" / "temporary.png"
    image.parent.mkdir()
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00temporary")
    _commit(repo, "add temporary image")
    image.unlink()
    allowlist = _write_file_allowlist(repo, "static/temporary.png")
    head = _commit(repo, "remove image and add exception")

    issues = scan_commit_range(
        repo,
        base,
        head,
        file_allowlist_path=allowlist,
    )

    assert {"binary-asset", "binary-file", "stale-file-exception"} <= _codes(issues)


def test_intermediate_large_blob_is_not_exempted_by_safe_head_blob(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    image = repo / "static" / "fixture.png"
    image.parent.mkdir()
    image.write_bytes(b"\x00" + (b"x" * 128))
    _commit(repo, "add dangerous image blob")
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00safe")
    allowlist = _write_file_allowlist(repo, "static/fixture.png")
    head = _commit(repo, "replace with reviewed image")

    issues = scan_commit_range(
        repo,
        base,
        head,
        file_allowlist_path=allowlist,
        max_bytes=64,
    )

    assert {"large-file", "binary-asset"} <= _codes(issues)


@pytest.mark.parametrize(
    ("path", "content", "expected_code"),
    [
        ("artifacts/allowed.sqlite3", b"SQLite format 3\x00fictional", "database-file"),
        (
            "keys/private.pem",
            b"-----BEGIN "
            + b"PRIVATE KEY-----\nfictional\n-----END PRIVATE KEY-----\n",
            "private-key-file",
        ),
        ("results/allowed.jsonl", b'{"item": "fictional"}\n', "runtime-result"),
    ],
)
def test_file_allowlist_cannot_exempt_sensitive_file_types(
    tmp_path,
    path,
    content,
    expected_code,
):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    artifact = repo / path
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(content)
    allowlist = _write_file_allowlist(repo, path)
    head = _commit(repo, "add forbidden allowlisted artifact")

    issues = scan_commit_range(
        repo,
        base,
        head,
        file_allowlist_path=allowlist,
    )

    assert expected_code in _codes(issues)


def test_head_blob_sha_mismatch_does_not_exempt_intermediate_blob(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    image = repo / "static" / "fixture.png"
    image.parent.mkdir()
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00first")
    _commit(repo, "add first image blob")
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00second")
    allowlist = _write_file_allowlist(repo, "static/fixture.png")
    head = _commit(repo, "replace image blob")

    issues = scan_commit_range(
        repo,
        base,
        head,
        file_allowlist_path=allowlist,
    )

    assert {"binary-asset", "binary-file"} <= _codes(issues)


def test_file_allowlist_cannot_mask_range_secret(tmp_path):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    candidate_value = "ghp_" + ("Q1w2E3r4" * 5)
    secret_file = repo / "static" / "reviewed.txt"
    secret_file.parent.mkdir()
    secret_file.write_text(f"token={candidate_value}\n", encoding="utf-8")
    allowlist = _write_file_allowlist(repo, "static/reviewed.txt")
    head = _commit(repo, "attempt file exception secret bypass")

    issues = scan_commit_range(
        repo,
        base,
        head,
        file_allowlist_path=allowlist,
    )

    assert "range-secret" in _codes(issues)
    assert candidate_value not in "\n".join(issue.render() for issue in issues)


@pytest.mark.parametrize(
    "entries",
    [
        "../outside.png\n",
        "static/*.png\n",
        "static/fixture.png\nstatic/fixture.png\n",
    ],
)
def test_range_file_allowlist_reuses_exact_path_validation(tmp_path, entries):
    repo = _init_repo(tmp_path)
    base = _base_commit(repo)
    allowlist = repo / "ci" / "public-file-allowlist.txt"
    allowlist.parent.mkdir()
    allowlist.write_text(entries, encoding="utf-8")
    head = _commit(repo, "add invalid allowlist")

    with pytest.raises(SafetyConfigError):
        scan_commit_range(
            repo,
            base,
            head,
            file_allowlist_path=allowlist,
        )
