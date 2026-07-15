from pathlib import Path

import pytest

from scripts.ci.check_pytest_baseline import (
    BaselineContractError,
    evaluate_baseline,
    load_allowlist,
    parse_junit,
)


KNOWN_ONE = "tests/unit/test_utils.py::test_save_to_jsonl"
KNOWN_TWO = (
    "tests/test_frontend_build_paths.py::"
    "test_frontend_build_output_path_is_consistent_across_configs"
)


def _write_junit(path: Path, *, failures=(), errors=(), passed=()) -> None:
    cases = []
    for nodeid, outcome in [
        *((nodeid, "failure") for nodeid in failures),
        *((nodeid, "error") for nodeid in errors),
        *((nodeid, "passed") for nodeid in passed),
    ]:
        result = "" if outcome == "passed" else f"<{outcome} message=\"fictional\" />"
        cases.append(
            "<testcase classname=\"ci.fixture\" name=\"case\">"
            f"<properties><property name=\"nodeid\" value=\"{nodeid}\" /></properties>"
            f"{result}</testcase>"
        )
    path.write_text(
        f"<testsuites><testsuite name=\"pytest\" tests=\"{len(cases)}\" "
        f"failures=\"{len(tuple(failures))}\" errors=\"{len(tuple(errors))}\">"
        + "".join(cases)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )


def test_exact_known_failures_satisfy_contract(tmp_path):
    junit = tmp_path / "results.xml"
    _write_junit(junit, failures=(KNOWN_ONE, KNOWN_TWO))
    report = parse_junit(junit, tmp_path)

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
