from scripts.ci.check_detect_secrets import SecretFinding, compare_findings


def _finding(path: str, hashed: str) -> SecretFinding:
    return SecretFinding(path, "High Entropy String", hashed, 7)


def test_detect_secret_baseline_exact_match_passes():
    finding = _finding("tests/fixture.py", "hash-one")

    assert compare_findings((finding,), (finding,)) == []


def test_detect_secret_baseline_rejects_new_finding_without_secret_value():
    baseline = _finding("tests/fixture.py", "hash-one")
    new = _finding("src/example.py", "fictional-raw-secret-must-not-print")

    problems = compare_findings((baseline,), (baseline, new))

    assert problems == ["new High Entropy String candidate at src/example.py:7"]
    assert new.hashed_secret not in problems[0]


def test_detect_secret_baseline_rejects_stale_finding():
    baseline = _finding("tests/fixture.py", "hash-one")

    assert compare_findings((baseline,), ()) == [
        "stale High Entropy String baseline entry at tests/fixture.py:7"
    ]
