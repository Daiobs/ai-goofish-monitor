#!/usr/bin/env python3
"""Compare a local detect-secrets scan with an exact reviewed baseline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.check_public_repo_safety import (
    SafetyConfigError,
    load_secret_allowlist,
)


class SecretScanError(RuntimeError):
    """Raised when the scanner or baseline cannot be evaluated safely."""


@dataclass(frozen=True)
class SecretFinding:
    path: str
    secret_type: str
    hashed_secret: str
    line_number: int

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.path, self.secret_type, self.hashed_secret)


def load_snapshot(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecretScanError(
            f"unable to parse secret snapshot ({type(exc).__name__})"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
        raise SecretScanError("secret snapshot has an invalid structure")
    return data


def snapshot_findings(data: dict) -> tuple[SecretFinding, ...]:
    findings: list[SecretFinding] = []
    for path, entries in data.get("results", {}).items():
        if not isinstance(path, str) or not isinstance(entries, list):
            raise SecretScanError("secret snapshot results are malformed")
        for entry in entries:
            if not isinstance(entry, dict):
                raise SecretScanError("secret snapshot finding is malformed")
            try:
                finding = SecretFinding(
                    path=Path(path).as_posix().removeprefix("./"),
                    secret_type=str(entry["type"]),
                    hashed_secret=str(entry["hashed_secret"]),
                    line_number=int(entry["line_number"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SecretScanError("secret snapshot finding is incomplete") from exc
            findings.append(finding)
    identities = [finding.identity for finding in findings]
    if len(identities) != len(set(identities)):
        raise SecretScanError("secret snapshot contains duplicate findings")
    return tuple(findings)


def compare_findings(
    baseline: tuple[SecretFinding, ...],
    actual: tuple[SecretFinding, ...],
) -> list[str]:
    baseline_map = {finding.identity: finding for finding in baseline}
    actual_map = {finding.identity: finding for finding in actual}
    problems: list[str] = []
    for identity in sorted(actual_map.keys() - baseline_map.keys()):
        finding = actual_map[identity]
        problems.append(
            f"new {finding.secret_type} candidate at {finding.path}:{finding.line_number}"
        )
    for identity in sorted(baseline_map.keys() - actual_map.keys()):
        finding = baseline_map[identity]
        problems.append(
            f"stale {finding.secret_type} baseline entry at {finding.path}:{finding.line_number}"
        )
    return problems


def validate_baseline_exceptions(
    repo_root: Path,
    baseline: tuple[SecretFinding, ...],
    secret_allowlist_path: Path,
) -> list[str]:
    try:
        rules = load_secret_allowlist(secret_allowlist_path)
    except (OSError, SafetyConfigError) as exc:
        raise SecretScanError(str(exc)) from exc
    problems: list[str] = []
    for finding in baseline:
        path = repo_root / finding.path
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            line = lines[finding.line_number - 1]
        except (OSError, UnicodeDecodeError, IndexError):
            problems.append(
                f"baseline source is unavailable at {finding.path}:{finding.line_number}"
            )
            continue
        allowed = any(
            rule.path == finding.path and rule.pattern.search(line)
            for rule in rules
        )
        if not allowed:
            problems.append(
                f"unreviewed baseline exception at {finding.path}:{finding.line_number}"
            )
    return problems


def git_tracked_paths(repo_root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SecretScanError("unable to enumerate tracked files for secret scan")
    return tuple(
        item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    )


def run_detect_secrets(repo_root: Path, tracked_paths: tuple[str, ...]) -> dict:
    with tempfile.TemporaryDirectory(prefix="detect-secrets-") as temp_dir:
        output_path = Path(temp_dir) / "scan.json"
        scan_paths = [
            f"./{path}"
            for path in tracked_paths
            if path != ".secrets.baseline"
        ]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "detect_secrets",
                "scan",
                "--no-verify",
                "--exclude-files",
                r"^\.secrets\.baseline$",
                *scan_paths,
            ],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SecretScanError(
                f"detect-secrets failed with status {result.returncode}"
            )
        output_path.write_text(result.stdout, encoding="utf-8")
        return load_snapshot(output_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--baseline", type=Path, default=Path(".secrets.baseline"))
    parser.add_argument(
        "--secret-allowlist",
        type=Path,
        default=Path("ci/public-secret-allowlist.yml"),
    )
    parser.add_argument(
        "--tracked-files-list",
        type=Path,
        help="newline-delimited tracked paths for isolated local validation",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        if args.tracked_files_list:
            tracked_paths = tuple(
                line.strip()
                for line in args.tracked_files_list.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        else:
            tracked_paths = git_tracked_paths(repo_root)
        baseline = snapshot_findings(load_snapshot(repo_root / args.baseline))
        actual = snapshot_findings(run_detect_secrets(repo_root, tracked_paths))
        problems = validate_baseline_exceptions(
            repo_root,
            baseline,
            repo_root / args.secret_allowlist,
        )
        problems.extend(compare_findings(baseline, actual))
    except (OSError, SecretScanError) as exc:
        print(f"secret scan failed: {exc}", file=sys.stderr)
        return 1
    if problems:
        for problem in problems:
            print(f"secret scan: {problem}", file=sys.stderr)
        return 1
    print(f"secret scan passed with {len(actual)} reviewed baseline finding(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
