from pathlib import Path

from scripts.ci.check_public_repo_safety import (
    inspect_tracked_file,
    inspect_workflow,
    load_secret_allowlist,
    scan_text_secrets,
    trusted_association_gate_issues,
)


PINNED_SHA = "01234567" * 5
ALLOWED_FAKE_TOKEN = "sk-test-ci-" + "fixture-1234567890abcdef"


def _codes(issues):
    return {issue.code for issue in issues}


def _inspect(tmp_path: Path, relative: str, content: bytes, *, max_bytes=1024 * 1024):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return inspect_tracked_file(
        tmp_path,
        relative,
        frozenset(),
        (),
        max_bytes=max_bytes,
    )


def test_env_file_is_rejected(tmp_path):
    assert "sensitive-file" in _codes(_inspect(tmp_path, ".env", b"TOKEN=fictional\n"))


def test_env_example_is_allowed(tmp_path):
    assert _inspect(tmp_path, ".env.example", b"TOKEN=replace-me\n") == []


def test_sqlite_file_is_rejected(tmp_path):
    assert "database-file" in _codes(
        _inspect(tmp_path, "data/app.sqlite3", b"SQLite format 3\x00fictional")
    )


def test_cookie_json_is_rejected(tmp_path):
    assert "cookie-file" in _codes(
        _inspect(tmp_path, "state/browser-cookies.json", b'{"cookie":"fictional"}')
    )


def test_exact_test_fixture_secret_exception_is_allowed():
    config = Path(__file__).resolve().parents[2] / "ci" / "public-secret-allowlist.yml"
    rules = load_secret_allowlist(config)

    assert scan_text_secrets(
        "tests/ci/test_public_repo_safety.py",
        ALLOWED_FAKE_TOKEN,
        rules,
    ) == []


def test_high_entropy_secret_is_rejected_without_echoing_value():
    value = "ghp_" + ("A1b2C3d4" * 5)

    issues = scan_text_secrets("src/example.py", value, ())

    assert _codes(issues) == {"secret-pattern"}
    assert value not in issues[0].render()


def _write_workflow(tmp_path: Path, text: str) -> Path:
    workflow = tmp_path / ".github" / "workflows" / "test.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(text, encoding="utf-8")
    return workflow


TRUSTED_COMMENT_GATE = """
      (github.event_name == 'issue_comment' &&
       contains(github.event.comment.body, '@claude') &&
       contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association))
""".strip()


def _comment_workflow(job: str) -> str:
    return f"""name: Test
on: {{issue_comment: {{types: [created]}}}}
permissions: {{contents: read}}
jobs:
  secure:
{job}
"""


def test_floating_action_is_rejected(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        """name: Test
on: {pull_request: {}}
permissions: {contents: read}
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
""",
    )

    assert "workflow-action-pin" in _codes(inspect_workflow(workflow, ".github/workflows/test.yml"))


def test_pinned_action_with_version_comment_is_allowed(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        f"""name: Test
on: {{pull_request: {{}}}}
permissions: {{contents: read}}
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{PINNED_SHA} # v7.0.0
""",
    )

    assert inspect_workflow(workflow, ".github/workflows/test.yml") == []


def test_pull_request_target_is_rejected(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        """name: Test
on: {pull_request_target: {}}
permissions: {contents: read}
jobs: {}
""",
    )

    assert "workflow-trigger" in _codes(inspect_workflow(workflow, ".github/workflows/test.yml"))


def test_default_write_permission_is_rejected(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        """name: Test
on: {pull_request: {}}
permissions:
  contents: read
  actions: write
jobs: {}
""",
    )

    assert "workflow-default-write" in _codes(inspect_workflow(workflow, ".github/workflows/test.yml"))


def test_oversized_binary_is_rejected(tmp_path):
    issues = _inspect(
        tmp_path,
        "models/weights.bin",
        b"\x00" + (b"x" * 128),
        max_bytes=64,
    )

    assert {"binary-artifact", "large-file"} <= _codes(issues)


def test_issue_comment_secret_without_association_gate_is_rejected(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        _comment_workflow(
            """    runs-on: ubuntu-latest
    env:
      TOKEN: ${{ secrets.TEST_TOKEN }}
    steps: []"""
        ),
    )

    assert "workflow-untrusted-trigger-gate" in _codes(
        inspect_workflow(workflow, ".github/workflows/test.yml")
    )


def test_body_only_comment_gate_is_rejected(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        _comment_workflow(
            """    if: contains(github.event.comment.body, '@claude')
    runs-on: ubuntu-latest
    env:
      TOKEN: ${{ secrets.TEST_TOKEN }}
    steps: []"""
        ),
    )

    assert "workflow-untrusted-trigger-gate" in _codes(
        inspect_workflow(workflow, ".github/workflows/test.yml")
    )


def test_trusted_owner_member_collaborator_gate_is_allowed(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        _comment_workflow(
            f"""    if: |
      {TRUSTED_COMMENT_GATE}
    runs-on: ubuntu-latest
    env:
      TOKEN: ${{{{ secrets.TEST_TOKEN }}}}
    steps: []"""
        ),
    )

    assert trusted_association_gate_issues(
        TRUSTED_COMMENT_GATE,
        {"issue_comment"},
    ) == []
    assert inspect_workflow(workflow, ".github/workflows/test.yml") == []


def test_contributor_association_is_not_a_trusted_gate(tmp_path):
    contributor_gate = TRUSTED_COMMENT_GATE.replace(
        "COLLABORATOR\"]",
        "COLLABORATOR\",\"CONTRIBUTOR\"]",
    )
    workflow = _write_workflow(
        tmp_path,
        _comment_workflow(
            f"""    if: |
      {contributor_gate}
    runs-on: ubuntu-latest
    env:
      TOKEN: ${{{{ secrets.TEST_TOKEN }}}}
    steps: []"""
        ),
    )

    assert "workflow-untrusted-trigger-gate" in _codes(
        inspect_workflow(workflow, ".github/workflows/test.yml")
    )


def test_comment_id_token_write_without_gate_is_rejected(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        _comment_workflow(
            """    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps: []"""
        ),
    )

    assert "workflow-untrusted-trigger-gate" in _codes(
        inspect_workflow(workflow, ".github/workflows/test.yml")
    )


def test_dynamic_bunx_in_secret_bearing_job_is_rejected(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        _comment_workflow(
            f"""    if: |
      {TRUSTED_COMMENT_GATE}
    runs-on: ubuntu-latest
    env:
      TOKEN: ${{{{ secrets.TEST_TOKEN }}}}
    steps:
      - run: bunx fictional-package start"""
        ),
    )

    assert "workflow-dynamic-package-exec" in _codes(
        inspect_workflow(workflow, ".github/workflows/test.yml")
    )


def test_locked_local_binary_in_secret_bearing_job_is_allowed(tmp_path):
    lock = tmp_path / "ci" / "router" / "package-lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    workflow = _write_workflow(
        tmp_path,
        _comment_workflow(
            f"""    if: |
      {TRUSTED_COMMENT_GATE}
    runs-on: ubuntu-latest
    env:
      TOKEN: ${{{{ secrets.TEST_TOKEN }}}}
    steps:
      - working-directory: ci/router
        run: npm ci --ignore-scripts
      - working-directory: ci/router
        run: ./node_modules/.bin/router start"""
        ),
    )

    assert inspect_workflow(workflow, ".github/workflows/test.yml") == []


def test_pull_request_job_may_not_receive_repository_secret(tmp_path):
    workflow = _write_workflow(
        tmp_path,
        """name: Test
on: {pull_request: {}}
permissions: {contents: read}
jobs:
  unsafe:
    runs-on: ubuntu-latest
    env:
      TOKEN: ${{ secrets.TEST_TOKEN }}
    steps: []
""",
    )

    assert "workflow-pr-secret" in _codes(
        inspect_workflow(workflow, ".github/workflows/test.yml")
    )


def test_repository_claude_workflow_is_gated_locked_and_has_no_oidc():
    repo_root = Path(__file__).resolve().parents[2]
    workflow = repo_root / ".github" / "workflows" / "claude.yml"
    text = workflow.read_text(encoding="utf-8")

    assert inspect_workflow(workflow, ".github/workflows/claude.yml") == []
    assert "id-token: write" not in text
    assert "bunx " not in text
    assert "./node_modules/.bin/ccr start" in text
