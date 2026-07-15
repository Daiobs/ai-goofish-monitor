from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from scripts.ci.check_pytest_baseline import (
    BaselineContractError,
    evaluate_baseline,
    load_allowlist,
    main,
    parse_junit,
)


KNOWN_ONE = "tests/unit/test_utils.py::test_save_to_jsonl"
KNOWN_TWO = (
    "tests/test_frontend_build_paths.py::"
    "test_frontend_build_output_path_is_consistent_across_configs"
)


def _write_junit(
    path: Path,
    *,
    failures=(),
    errors=(),
    skipped=(),
    xfailed=(),
    passed=(),
    failure_body="",
) -> None:
    failures = tuple(failures)
    errors = tuple(errors)
    skipped = tuple(skipped)
    xfailed = tuple(xfailed)
    passed = tuple(passed)
    cases = []
    for nodeid, outcome in [
        *((nodeid, "failure") for nodeid in failures),
        *((nodeid, "error") for nodeid in errors),
        *((nodeid, "skipped") for nodeid in skipped),
        *((nodeid, "xfailed") for nodeid in xfailed),
        *((nodeid, "passed") for nodeid in passed),
    ]:
        if outcome == "passed":
            result = ""
        elif outcome == "xfailed":
            result = '<skipped type="pytest.xfail" message="fictional" />'
        elif outcome == "skipped":
            result = '<skipped type="pytest.skip" message="fictional" />'
        elif outcome == "failure":
            result = (
                '<failure message="fictional">'
                + escape(failure_body)
                + "</failure>"
            )
        else:
            result = f"<{outcome} message=\"fictional\" />"
        cases.append(
            "<testcase classname=\"ci.fixture\" name=\"case\">"
            f"<properties><property name=\"nodeid\" value=\"{nodeid}\" /></properties>"
            f"{result}</testcase>"
        )
    path.write_text(
        f"<testsuites><testsuite name=\"pytest\" tests=\"{len(cases)}\" "
        f"failures=\"{len(failures)}\" errors=\"{len(errors)}\" "
        f"skipped=\"{len(skipped) + len(xfailed)}\">"
        + "".join(cases)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )


def test_exact_known_failures_satisfy_contract(tmp_path):
    junit = tmp_path / "results.xml"
    _write_junit(junit, failures=(KNOWN_ONE, KNOWN_TWO))
    report = parse_junit(junit, tmp_path)

    assert report.skipped == frozenset()
    assert evaluate_baseline(report, frozenset((KNOWN_ONE, KNOWN_TWO)), 1) == []


def test_unknown_failure_breaks_contract(tmp_path):
    junit = tmp_path / "results.xml"
    unknown = "tests/unit/test_example.py::test_new_failure"
    _write_junit(junit, failures=(KNOWN_ONE, KNOWN_TWO, unknown))

    problems = evaluate_baseline(
        parse_junit(junit, tmp_path),
        frozenset((KNOWN_ONE, KNOWN_TWO)),
        1,
    )

    assert any(unknown in problem and "new test failures" in problem for problem in problems)


def test_known_failure_becoming_pass_breaks_contract(tmp_path):
    junit = tmp_path / "results.xml"
    _write_junit(junit, failures=(KNOWN_ONE,), passed=(KNOWN_TWO,))

    problems = evaluate_baseline(
        parse_junit(junit, tmp_path),
        frozenset((KNOWN_ONE, KNOWN_TWO)),
        1,
    )

    assert any(KNOWN_TWO in problem and "stale allowlist" in problem for problem in problems)


def test_collection_or_fixture_error_breaks_contract(tmp_path):
    junit = tmp_path / "results.xml"
    error_nodeid = "tests/unit/test_example.py::test_broken_fixture"
    _write_junit(junit, failures=(KNOWN_ONE, KNOWN_TWO), errors=(error_nodeid,))

    problems = evaluate_baseline(
        parse_junit(junit, tmp_path),
        frozenset((KNOWN_ONE, KNOWN_TWO)),
        1,
    )

    assert any("collection, fixture, or internal errors" in problem for problem in problems)


def test_collection_error_without_testcases_is_rejected(tmp_path):
    junit = tmp_path / "results.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest" tests="0" failures="0" errors="1" />'
        '</testsuites>',
        encoding="utf-8",
    )

    with pytest.raises(BaselineContractError, match="no test cases"):
        parse_junit(junit, tmp_path)


def test_corrupt_junit_is_rejected(tmp_path):
    junit = tmp_path / "results.xml"
    junit.write_text("<broken", encoding="utf-8")

    with pytest.raises(BaselineContractError, match="unable to parse"):
        parse_junit(junit, tmp_path)


def test_empty_allowlist_is_valid(tmp_path):
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("\n", encoding="utf-8")

    assert load_allowlist(allowlist) == frozenset()


def test_duplicate_allowlist_entry_is_rejected(tmp_path):
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text(f"{KNOWN_ONE}\n{KNOWN_ONE}\n", encoding="utf-8")

    with pytest.raises(BaselineContractError, match="duplicate"):
        load_allowlist(allowlist)


def test_normal_skip_breaks_contract_and_preserves_nodeid(tmp_path):
    junit = tmp_path / "results.xml"
    skipped = "tests/unit/test_example.py::test_skipped"
    _write_junit(
        junit,
        failures=(KNOWN_ONE, KNOWN_TWO),
        skipped=(skipped,),
    )
    report = parse_junit(junit, tmp_path)

    problems = evaluate_baseline(
        report,
        frozenset((KNOWN_ONE, KNOWN_TWO)),
        1,
    )

    assert report.skipped == frozenset({skipped})
    assert any(skipped in problem and "forbidden" in problem for problem in problems)


def test_xfail_skip_breaks_contract(tmp_path):
    junit = tmp_path / "results.xml"
    xfailed = "tests/unit/test_example.py::test_expected_failure"
    _write_junit(
        junit,
        failures=(KNOWN_ONE, KNOWN_TWO),
        xfailed=(xfailed,),
    )

    problems = evaluate_baseline(
        parse_junit(junit, tmp_path),
        frozenset((KNOWN_ONE, KNOWN_TWO)),
        1,
    )

    assert any(xfailed in problem and "xfailed" in problem for problem in problems)


def test_multiple_skips_are_all_reported(tmp_path):
    junit = tmp_path / "results.xml"
    skipped = (
        "tests/unit/test_example.py::test_first_skip",
        "tests/unit/test_example.py::test_second_skip",
    )
    _write_junit(
        junit,
        failures=(KNOWN_ONE, KNOWN_TWO),
        skipped=skipped,
    )
    report = parse_junit(junit, tmp_path)

    assert report.skipped == frozenset(skipped)
    problems = evaluate_baseline(
        report,
        frozenset((KNOWN_ONE, KNOWN_TWO)),
        1,
    )
    assert all(any(nodeid in problem for problem in problems) for nodeid in skipped)


def test_summary_never_copies_junit_failure_body(tmp_path):
    junit = tmp_path / "results.xml"
    allowlist = tmp_path / "allowlist.txt"
    summary = tmp_path / "summary.txt"
    candidate_value = "ghp_" + ("Q1w2E3r4" * 5)
    _write_junit(
        junit,
        failures=(KNOWN_ONE, KNOWN_TWO),
        failure_body=f"synthetic failure body contains {candidate_value}",
    )
    allowlist.write_text(f"{KNOWN_ONE}\n{KNOWN_TWO}\n", encoding="utf-8")

    assert main(
        [
            "--junit",
            str(junit),
            "--allowlist",
            str(allowlist),
            "--pytest-exit-code",
            "1",
            "--summary",
            str(summary),
            "--repo-root",
            str(tmp_path),
        ]
    ) == 0
    assert candidate_value not in summary.read_text(encoding="utf-8")


def test_quality_workflow_enforces_strict_xpass_and_uploads_only_summary():
    repo_root = Path(__file__).resolve().parents[2]
    workflow = (repo_root / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )
    upload_block = workflow.split(
        "- name: Upload sanitized pytest baseline summary",
        1,
    )[1].split("\n  frontend-build:", 1)[0]

    assert "-o xfail_strict=true" in workflow
    assert "path: artifacts/pytest-summary.txt" in upload_block
    assert "pytest-results.xml" not in upload_block
