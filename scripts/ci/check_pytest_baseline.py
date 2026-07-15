#!/usr/bin/env python3
"""Enforce an exact pytest failure baseline from a JUnit XML report."""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class BaselineContractError(RuntimeError):
    """Raised when pytest output does not satisfy the baseline contract."""


@dataclass(frozen=True)
class JUnitReport:
    failures: frozenset[str]
    errors: frozenset[str]
    skipped: frozenset[str]
    tests: int


DIAGNOSTIC_SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(?:api[-_]?key|access[-_]?token|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@"),
)


def sanitize_diagnostic(value: str) -> str:
    sanitized = value
    for pattern in DIAGNOSTIC_SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def load_allowlist(path: Path) -> frozenset[str]:
    entries = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    entries = [entry for entry in entries if entry]
    duplicates = sorted({entry for entry in entries if entries.count(entry) > 1})
    if duplicates:
        raise BaselineContractError(
            "duplicate allowlist entries: " + ", ".join(duplicates)
        )
    invalid = sorted(entry for entry in entries if not entry.startswith("tests/") or "::" not in entry)
    if invalid:
        raise BaselineContractError(
            "invalid allowlist node IDs: " + ", ".join(invalid)
        )
    return frozenset(entries)


def _property_nodeid(testcase: ET.Element) -> str | None:
    for prop in testcase.findall("./properties/property"):
        if prop.get("name") == "nodeid" and prop.get("value"):
            return prop.get("value")
    return None


def _fallback_nodeid(testcase: ET.Element, repo_root: Path) -> str:
    classname = testcase.get("classname", "")
    test_name = testcase.get("name", "<unknown>")
    parts = classname.split(".") if classname else []

    for index in range(len(parts), 0, -1):
        candidate = Path(*parts[:index]).with_suffix(".py")
        if (repo_root / candidate).is_file():
            suffix = parts[index:] + [test_name]
            return f"{candidate.as_posix()}::{'::'.join(suffix)}"

    return f"<junit>::{classname}::{test_name}"


def _testcase_nodeid(testcase: ET.Element, repo_root: Path) -> str:
    return _property_nodeid(testcase) or _fallback_nodeid(testcase, repo_root)


def parse_junit(path: Path, repo_root: Path) -> JUnitReport:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise BaselineContractError(
            f"unable to parse JUnit report ({type(exc).__name__})"
        ) from exc

    tests = 0
    skipped: set[str] = set()
    failures: set[str] = set()
    errors: set[str] = set()

    for suite in root.iter("testsuite"):
        try:
            if int(suite.get("errors", "0")) > 0:
                errors.add(f"<testsuite>::{suite.get('name', '<unknown>')}")
        except ValueError as exc:
            raise BaselineContractError("invalid JUnit testsuite counters") from exc

    for testcase in root.iter("testcase"):
        tests += 1
        nodeid = _testcase_nodeid(testcase, repo_root)
        if testcase.find("error") is not None:
            errors.add(nodeid)
        elif testcase.find("failure") is not None:
            failures.add(nodeid)
        if testcase.find("skipped") is not None:
            skipped.add(nodeid)

    if tests == 0:
        raise BaselineContractError("JUnit report contains no test cases")

    return JUnitReport(
        failures=frozenset(failures),
        errors=frozenset(errors),
        skipped=frozenset(skipped),
        tests=tests,
    )


def evaluate_baseline(
    report: JUnitReport,
    allowlist: frozenset[str],
    pytest_exit_code: int,
) -> list[str]:
    problems: list[str] = []
    if pytest_exit_code not in {0, 1}:
        problems.append(f"pytest exited with unsupported status {pytest_exit_code}")
    if report.errors:
        problems.append("pytest collection, fixture, or internal errors: " + ", ".join(sorted(report.errors)))
    if report.skipped:
        problems.append(
            "skipped or xfailed tests are forbidden in required CI: "
            + ", ".join(sorted(report.skipped))
        )

    unknown = sorted(report.failures - allowlist)
    stale = sorted(allowlist - report.failures)
    if unknown:
        problems.append("new test failures: " + ", ".join(unknown))
    if stale:
        problems.append(
            "stale allowlist entries now pass; remove them: " + ", ".join(stale)
        )

    expected_exit_code = 1 if report.failures or report.errors else 0
    if pytest_exit_code in {0, 1} and pytest_exit_code != expected_exit_code:
        problems.append(
            "pytest exit status is inconsistent with the JUnit failure set"
        )
    return problems


def _write_summary(
    path: Path,
    report: JUnitReport | None,
    pytest_exit_code: int,
    problems: Iterable[str],
) -> None:
    problems = list(problems)
    lines = [
        f"contract={'failed' if problems else 'passed'}",
        f"pytest_exit_code={pytest_exit_code}",
    ]
    if report is not None:
        lines.extend(
            [
                f"tests={report.tests}",
                f"skipped={len(report.skipped)}",
                f"failures={len(report.failures)}",
                f"errors={len(report.errors)}",
            ]
        )
        lines.extend(f"failure_nodeid={nodeid}" for nodeid in sorted(report.failures))
        lines.extend(f"error_nodeid={nodeid}" for nodeid in sorted(report.errors))
        lines.extend(f"skipped_nodeid={nodeid}" for nodeid in sorted(report.skipped))
    lines = [sanitize_diagnostic(line) for line in lines]
    lines.extend(f"problem={sanitize_diagnostic(problem)}" for problem in problems)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--pytest-exit-code", type=int, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report: JUnitReport | None = None
    problems: list[str] = []
    try:
        allowlist = load_allowlist(args.allowlist)
        report = parse_junit(args.junit, args.repo_root)
        problems.extend(evaluate_baseline(report, allowlist, args.pytest_exit_code))
    except (OSError, BaselineContractError) as exc:
        problems.append(str(exc))

    _write_summary(args.summary, report, args.pytest_exit_code, problems)
    if problems:
        for problem in problems:
            print(f"baseline contract: {sanitize_diagnostic(problem)}", file=sys.stderr)
        return 1

    print(
        "baseline contract passed: "
        f"{report.tests} tests, {len(report.skipped)} skipped, "
        f"{len(report.failures)} expected failures"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
